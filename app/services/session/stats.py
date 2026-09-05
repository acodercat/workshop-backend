"""Aggregate reporting over ``session_turn``.

Its own module for the reason :func:`app.services.check_in.count_in_window`
and :func:`app.services.user.stats.count_registered_since` are: tokens
are measured on the turn, not on the ledger, and folding a non-ledger
figure into ``/stats/credits`` would make the credits module answer for a
table it does not own.

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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
