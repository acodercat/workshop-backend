"""Aggregate reporting over ``session`` and ``session_turn``.

Its own module for the reason :func:`app.services.check_in.count_in_window`
and :func:`app.services.user.stats.count_registered_since` are: tokens
are measured on the turn and conversations on the session, not on the
ledger, and folding a non-ledger figure into ``/stats/credits`` would
make the credits module answer for a table it does not own.

The window is cut on ``terminated_at``, not ``created_at``: a turn's
token counts are written when it reaches a terminal that measured them,
so that is the moment the consumption belongs to. ``updated_at`` would
be wrong twice over — lease heartbeats bump it, and a long turn would
drift into whichever window last touched it.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session
from app.models.session_turn import SessionTurn


class TurnUsage(NamedTuple):
    """What one session ran up: turns taken, tokens measured."""

    turn_count: int
    total_tokens: int


NO_TURN_USAGE = TurnUsage(turn_count=0, total_tokens=0)
"""Default for a session absent from :func:`per_session_usage`.

A session with no turns has no row in the grouped result, and every
caller renders a full page — so the miss is expected, not exceptional.
"""


async def count_tokens_since(db: AsyncSession, *, since: datetime) -> tuple[int, int]:
    """``(input, output)`` tokens measured on turns terminated since ``since``.

    The two columns are coalesced separately rather than summed and then
    coalesced: ``sum(input + output)`` is NULL for the whole set the
    moment one row has a NULL half, which would discard the measured half
    with it. Both stay NULL when a turn ended without measuring anything
    (a worker crash, a pre-V2 row), and a turn that measured nothing
    contributes nothing rather than skewing the total.
    """
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(SessionTurn.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(SessionTurn.output_tokens), 0).label("output_tokens"),
            ).where(SessionTurn.terminated_at >= since)
        )
    ).one()
    return int(row.input_tokens), int(row.output_tokens)


async def per_session_usage(
    db: AsyncSession, session_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, TurnUsage]:
    """Turns run and tokens burned per session, in one grouped query.

    One query for a whole page rather than two per row, and its own
    query rather than a join onto the session page: each domain answers
    its own question over the page's ids and the caller stitches the
    answers by id, the same shape
    :func:`app.services.credits.analytics.session_spend` uses for money.
    A single wide join across sessions, turns and the ledger would have
    to live somewhere, and no module owns all three.

    The two token columns are coalesced **separately** for the reason
    :func:`count_tokens_since` gives, with one addition that only
    matters per session: they are NULL until a turn reaches a terminal
    that measured usage, so a turn still running counts toward
    ``turn_count`` and contributes no tokens — the honest reading of a
    measurement that has not happened.
    """
    if not session_ids:
        return {}
    total_tokens = func.coalesce(func.sum(SessionTurn.input_tokens), 0) + func.coalesce(
        func.sum(SessionTurn.output_tokens), 0
    )
    rows = (
        await db.execute(
            select(
                SessionTurn.session_id,
                func.count().label("turn_count"),
                total_tokens.label("total_tokens"),
            )
            .where(SessionTurn.session_id.in_(session_ids))
            .group_by(SessionTurn.session_id)
        )
    ).all()
    return {
        row.session_id: TurnUsage(
            turn_count=int(row.turn_count), total_tokens=int(row.total_tokens)
        )
        for row in rows
    }


def _conversations_opened_since(since: datetime) -> tuple[ColumnElement[bool], ...]:
    """What a conversation is, for every count that asks.

    A session opened since ``since`` that somebody actually spoke in. The
    client creates the row before the first message goes out, so a first
    send that fails (no credits, a rejected upload) leaves a turnless
    shell behind — and the sibling rankings count things that happened,
    not attempts. Cut on ``created_at``: the question is "how many
    conversations were started", and one opened in the window and still
    going counts. (The session *list* cuts on ``updated_at`` — it shows
    and sorts by last activity.) Soft-deleted sessions count too, as they
    do on the platform-wide list — deleted is not the same as never
    happened.

    Its own function so the decision reads once, and so a second count
    over sessions — per model, say — starts from the same definition
    rather than drifting into its own.
    """
    return (
        Session.created_at >= since,
        exists().where(SessionTurn.session_id == Session.id),
    )


def opened_sessions_query(*, since: datetime) -> Select[tuple[int, int]]:
    """The statement behind :func:`count_opened_since`, built apart so a
    test can compile it and read the SQL rather than the source."""
    return select(
        func.count().label("sessions"),
        func.count(func.distinct(Session.user_id)).label("users"),
    ).where(*_conversations_opened_since(since))


async def count_opened_since(db: AsyncSession, *, since: datetime) -> tuple[int, int]:
    """``(conversations, distinct users)`` opened since ``since``.

    No join onto ``user``: this counts conversations, and who owns one is
    not part of the question. ``users`` is ``COUNT(DISTINCT user_id)``,
    which skips a NULL owner rather than counting it as a person —
    ``session.user_id`` is ``ON DELETE CASCADE``, so an ownerless row is
    not expected, and this is written not to lie if one appears.
    """
    row = (await db.execute(opened_sessions_query(since=since))).one()
    return int(row.sessions), int(row.users)
