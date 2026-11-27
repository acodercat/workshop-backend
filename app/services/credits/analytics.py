"""Aggregate reporting over the credit ledger.

Read-only, and deliberately in the ``credits`` package rather than under
``services/admin``: these are questions about the ledger, so the domain
that owns the ledger owns them — the same placement as
:mod:`~app.services.credits.transactions`. The console is the first
caller; a customer-facing "your usage this month" would reuse them.

**How spend is counted.** A turn writes two or three ledger rows, not
one: ``RESERVE -estimate`` at enqueue, then at settle ``SETTLE
+estimate`` (releasing the hold) and ``SETTLE -actual`` (the real
charge); a turn that never measured usage gets ``REFUND +estimate``
instead. So the only row that represents money actually consumed is the
**negative SETTLE**, and every figure here derives from it::

    spend = SUM(-amount) WHERE kind = 'SETTLE' AND amount < 0

Summing ``RESERVE`` would report estimates rather than actuals, and
summing every turn-related kind would smear one turn across two days
whenever it straddles midnight. Counting negative SETTLE rows also makes
"turns" exact — one settled turn, one row.

**Business days.** Daily buckets are cut in the business timezone
(:mod:`app.common.business_time`), not UTC, so "today" means what an
operator means by it — the same rule the daily check-in already uses.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.business_time import business_days, business_tz, window_start

# The single predicate every spend figure is built on — see the module
# docstring. Kept as one string so the four queries below cannot drift
# into counting different things.
_SPEND_ROWS = "kind = 'SETTLE' AND amount < 0"

# Grants that add spendable credits. ``ADJUST`` is signed, so only its
# positive half is a grant; the negative half is a clawback and is
# reported separately rather than netted away.
_GRANT_ROWS = (
    "((kind IN ('TOPUP', 'REFERRAL', 'CHECKIN') AND amount > 0)"
    " OR (kind = 'ADJUST' AND amount > 0))"
)
_CLAWBACK_ROWS = "kind = 'ADJUST' AND amount < 0"

# A grant the customer actually **paid for**. The marker is the payment
# order's back-link, not the ledger kind: ``TOPUP`` is what
# ``apply_topup`` writes for *every* balance increase it handles — a
# purchase, the registration welcome grant, an operator giveaway
# (``scripts/topup_credits.py``), a commission conversion — so reading
# ``kind = 'TOPUP'`` as "sold" books each of those as revenue, and the
# welcome grant alone moves it by the full signup volume.
# ``payment_order.ledger_id`` is set only on the PENDING→PAID flip, so
# it is the one structural record that money changed hands — and it
# stays right when a second provider lands, which keying on the ``hf:``
# idempotency prefix would not.
#
# Uncorrelated ``IN`` rather than a correlated ``EXISTS`` because this
# sits inside an aggregate ``FILTER``, where a correlated subquery
# re-runs per row instead of being pulled up into a semi-join; this form
# lets Postgres hash the (small) order table once. The ``IS NOT NULL``
# guard is load-bearing, not decoration: a NULL in the subquery would
# make the negation below evaluate to NULL for every unmatched row and
# silently report zero free grants.
_SOLD_ROWS = "id IN (SELECT ledger_id FROM payment_order WHERE ledger_id IS NOT NULL)"

# The share of grants the platform *gave away* rather than sold. Defined
# against ``_GRANT_ROWS`` rather than by listing kinds, so a new grant
# kind counts as free unless it is a purchase. Without this split,
# "granted" moves for opposite reasons — a good sales day and an
# expensive giveaway look identical — and the sold half is already
# reported in ¥ by the payments summary.
_FREE_GRANT_ROWS = f"{_GRANT_ROWS} AND NOT ({_SOLD_ROWS})"


@dataclass(frozen=True, slots=True)
class DailySpend:
    """One business day's consumption. Days with no spend are present
    with ``spend_micro = 0`` — a gap in a chart should read as a quiet
    day, not as missing data."""

    day: date
    spend_micro: int


@dataclass(frozen=True, slots=True)
class ModelSpend:
    """Consumption attributed to one model, via the settled
    reservation's session."""

    model_id: str
    spend_micro: int


@dataclass(frozen=True, slots=True)
class CreditsSummary:
    """Everything the dashboard's tiles and chart need, for one window."""

    spend_micro: int
    granted_micro: int
    """Every credit added: purchases and giveaways together — the
    liability increase."""
    granted_free_micro: int
    """The giveaway half of it — every grant with no paid order behind
    it, so welcome grants, check-ins, referral rewards, operator top-ups
    and commission conversions all land here. Reported beside the total
    because the two move for opposite reasons; the remainder
    (``granted_micro - granted_free_micro``) is what was sold."""
    clawed_back_micro: int
    active_users: int
    settled_turns: int
    outstanding_micro: int
    daily: list[DailySpend]
    by_model: list[ModelSpend]


class UserActivityOrder(StrEnum):
    """How to rank the same per-user aggregate.

    Two questions, one query: *who spends most* (volume) and *who used it
    last* (recency) differ only in ``ORDER BY``, so they share the
    grouping rather than drifting into two definitions of "active".
    """

    SPEND = "spend"
    RECENT = "recent"


@dataclass(frozen=True, slots=True)
class UserActivity:
    """One user's consumption over the window, for either ranking."""

    user_id: int
    username: str
    name: str
    spend_micro: int
    settled_turns: int
    balance_micro: int
    last_spend_at: datetime


async def summarize(db: AsyncSession, *, days: int) -> CreditsSummary:
    """Aggregate the ledger over the trailing ``days`` business days."""
    since = window_start(days)
    totals = (
        await db.execute(
            text(f"""
            SELECT
                COALESCE(SUM(-amount) FILTER (WHERE {_SPEND_ROWS}), 0)
                    AS spend_micro,
                COALESCE(SUM(amount) FILTER (WHERE {_GRANT_ROWS}), 0)
                    AS granted_micro,
                COALESCE(SUM(amount) FILTER (WHERE {_FREE_GRANT_ROWS}), 0)
                    AS granted_free_micro,
                COALESCE(SUM(-amount) FILTER (WHERE {_CLAWBACK_ROWS}), 0)
                    AS clawed_back_micro,
                COUNT(DISTINCT user_id) FILTER (WHERE {_SPEND_ROWS})
                    AS active_users,
                COUNT(*) FILTER (WHERE {_SPEND_ROWS}) AS settled_turns
            FROM credit_ledger
            WHERE created_at >= :since
            """),
            {"since": since},
        )
    ).one()

    # Outstanding prepaid liability — every user's spendable balance.
    # Not windowed: it is a point-in-time position, not a flow.
    outstanding = (
        await db.execute(text('SELECT COALESCE(SUM(credit_balance), 0) FROM "user"'))
    ).scalar_one()

    return CreditsSummary(
        spend_micro=int(totals.spend_micro),
        granted_micro=int(totals.granted_micro),
        granted_free_micro=int(totals.granted_free_micro),
        clawed_back_micro=int(totals.clawed_back_micro),
        active_users=int(totals.active_users),
        settled_turns=int(totals.settled_turns),
        outstanding_micro=int(outstanding),
        daily=await _daily_spend(db, since=since, days=days),
        by_model=await _spend_by_model(db, since=since),
    )


# The two rankings, kept beside each other so it is obvious they differ
# only in ordering. Not caller-supplied text — an ``ORDER BY`` fragment
# reaching SQL from a request would be an injection.
_ACTIVITY_ORDER_BY = {
    UserActivityOrder.SPEND: "spend_micro DESC",
    UserActivityOrder.RECENT: "last_spend_at DESC",
}


async def user_activity(
    db: AsyncSession,
    *,
    days: int,
    limit: int,
    order: UserActivityOrder,
) -> list[UserActivity]:
    """Per-user consumption over the window, ranked by ``order``.

    ``SPEND`` answers "who costs the most", ``RECENT`` answers "who is
    still using it" — the same aggregate seen from two angles, so a user
    can never be "active" by one definition and absent from the other.
    """
    rows = (
        await db.execute(
            text(f"""
            SELECT
                l.user_id,
                u.username,
                u.name,
                SUM(-l.amount) AS spend_micro,
                COUNT(*)       AS settled_turns,
                u.credit_balance,
                MAX(l.created_at) AS last_spend_at
            FROM credit_ledger l
            JOIN "user" u ON u.id = l.user_id
            WHERE {_SPEND_ROWS} AND l.created_at >= :since
            GROUP BY l.user_id, u.username, u.name, u.credit_balance
            ORDER BY {_ACTIVITY_ORDER_BY[order]}
            LIMIT :limit
            """),
            {"since": window_start(days), "limit": limit},
        )
    ).all()
    return [
        UserActivity(
            user_id=row.user_id,
            username=row.username,
            name=row.name,
            spend_micro=int(row.spend_micro),
            settled_turns=int(row.settled_turns),
            balance_micro=int(row.credit_balance),
            last_spend_at=row.last_spend_at,
        )
        for row in rows
    ]


async def session_spend(db: AsyncSession, session_ids: Sequence[UUID]) -> dict[UUID, int]:
    """What each of the given sessions actually consumed, in µ-credits.

    Routed ledger row → reservation → session, the same hop
    :func:`_spend_by_model` makes: a ledger row names a reservation, and
    only the reservation names the session.

    Counts negative ``SETTLE`` rows like every other figure here, so a
    turn still running contributes nothing — its ``RESERVE`` is an
    estimate, and reporting estimates beside actuals in one column is how
    a list stops meaning anything.

    A session with nothing settled is **absent** rather than zero; the
    caller renders a page and supplies its own default.
    """
    if not session_ids:
        return {}
    rows = (
        await db.execute(
            text(f"""
            SELECT r.session_id AS session_id, SUM(-l.amount) AS spend_micro
            FROM credit_ledger l
            JOIN credit_reservation r ON r.id = l.reservation_id
            WHERE {_SPEND_ROWS} AND r.session_id = ANY(:session_ids)
            GROUP BY r.session_id
            """),
            {"session_ids": list(session_ids)},
        )
    ).all()
    return {row.session_id: int(row.spend_micro) for row in rows}


async def _daily_spend(db: AsyncSession, *, since: datetime, days: int) -> list[DailySpend]:
    """Per-business-day consumption, gap-filled to a contiguous series."""
    rows = (
        await db.execute(
            text(f"""
            SELECT
                (created_at AT TIME ZONE :tz)::date AS day,
                SUM(-amount) AS spend_micro
            FROM credit_ledger
            WHERE {_SPEND_ROWS} AND created_at >= :since
            GROUP BY day
            """),
            {"since": since, "tz": business_tz().key},
        )
    ).all()

    spend_by_day = {row.day: int(row.spend_micro) for row in rows}
    return [
        DailySpend(day=day, spend_micro=spend_by_day.get(day, 0)) for day in business_days(days)
    ]


async def _spend_by_model(db: AsyncSession, *, since: datetime) -> list[ModelSpend]:
    """Consumption grouped by the model that ran the turn.

    Routed ledger row → reservation → session, because ``model_id`` lives
    on the session (it is the billing truth the rate card is keyed on).
    A settled row whose session was hard-deleted falls into ``unknown``
    rather than vanishing, so the parts still sum to the total.
    """
    rows = (
        await db.execute(
            text(f"""
            SELECT
                COALESCE(s.model_id, 'unknown') AS model_id,
                SUM(-l.amount) AS spend_micro
            FROM credit_ledger l
            LEFT JOIN credit_reservation r ON r.id = l.reservation_id
            LEFT JOIN session s ON s.id = r.session_id
            WHERE {_SPEND_ROWS} AND l.created_at >= :since
            GROUP BY COALESCE(s.model_id, 'unknown')
            ORDER BY spend_micro DESC
            """),
            {"since": since},
        )
    ).all()
    return [ModelSpend(model_id=row.model_id, spend_micro=int(row.spend_micro)) for row in rows]
