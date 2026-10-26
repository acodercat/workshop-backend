"""Daily check-in — claim a fixed credit reward once per business day.

Orchestration over the credit ledger: a claim inserts the paired
``CHECKIN`` credit_ledger row (via :func:`credits.apply_check_in_reward`)
and a ``check_in`` audit row in one transaction. "One per day" is
DB-enforced twice — the ``check_in`` UNIQUE(user_id, date) and the
ledger ``idempotency_key`` — so a double-click / retry is race-safe.

The "day" is the calendar day in the business timezone
(``settings.checkin_timezone``, Asia/Shanghai), computed here rather
than by the DB so an early-morning claim lands on the right day.

``streak_day`` is the consecutive-day count; it is recorded for display
("已连续签到 N 天") but does **not** change the reward in this MVP.

Every function takes an ``AsyncSession`` and **does not commit** — the
caller (endpoint) owns the transaction, matching the ``credits`` /
``commission`` service contract.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import NamedTuple

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.business_time import business_today
from app.errors import AlreadyCheckedInError, CheckInDisabledError
from app.models.check_in import CheckIn
from app.services import credits
from app.services.credits.check_in import CheckInAlreadyClaimed
from app.services.credits.rates import micro_from_credits
from config import settings

logger = logging.getLogger(__name__)


class CheckInStatus(NamedTuple):
    """Snapshot for ``GET /check-ins/today``. ``streak_day`` is the
    current consecutive-day streak; ``total_days`` is the lifetime
    check-in count (the "累计签到" display)."""

    enabled: bool
    checked_in_today: bool
    streak_day: int
    total_days: int
    reward_micro: int


class ClaimedCheckIn(NamedTuple):
    """Result of a successful claim (amounts in µ-credits; the endpoint
    converts to whole credits and reads the fresh balance)."""

    reward_micro: int
    streak_day: int


async def _latest_check_in(db: AsyncSession, user_id: int) -> CheckIn | None:
    """The user's most recent check-in row (by date), or ``None``. Served
    by the ``uq_check_in_user_date`` index via a backward scan."""
    return await db.scalar(
        select(CheckIn)
        .where(CheckIn.user_id == user_id)
        .order_by(CheckIn.check_in_date.desc())
        .limit(1)
    )


def _current_streak(latest: CheckIn | None, today: date) -> int:
    """Consecutive-day streak still standing as of the last check-in, or
    ``0`` if there's none or it lapsed (last check-in older than
    yesterday). A claim today extends this by one."""
    if latest is None:
        return 0
    if latest.check_in_date in (today, today - timedelta(days=1)):
        return latest.streak_day
    return 0


async def get_check_in_status(db: AsyncSession, *, user_id: int) -> CheckInStatus:
    """Return today's check-in state for ``user_id`` (read-only)."""
    today = business_today()
    latest = await _latest_check_in(db, user_id)
    total_days = (
        await db.scalar(select(func.count(CheckIn.id)).where(CheckIn.user_id == user_id)) or 0
    )
    return CheckInStatus(
        enabled=settings.checkin_reward_credits > 0,
        checked_in_today=latest is not None and latest.check_in_date == today,
        streak_day=_current_streak(latest, today),
        total_days=total_days,
        reward_micro=micro_from_credits(settings.checkin_reward_credits),
    )


async def claim_daily_check_in(
    db: AsyncSession,
    *,
    user_id: int,
) -> ClaimedCheckIn:
    """Claim today's check-in: grant the reward + record the ``check_in``
    row, atomic. The caller commits.

    Raises :class:`CheckInDisabledError` if the feature is off, or
    :class:`AlreadyCheckedInError` if today is already claimed (fast path
    on the pre-read, and the DB double-guard on a concurrent claim).
    """
    if settings.checkin_reward_credits <= 0:
        raise CheckInDisabledError("Daily check-in is not enabled")

    today = business_today()
    latest = await _latest_check_in(db, user_id)
    if latest is not None and latest.check_in_date == today:
        raise AlreadyCheckedInError("Already checked in today")

    reward_micro = micro_from_credits(settings.checkin_reward_credits)
    streak_day = _current_streak(latest, today) + 1

    try:
        entry = await credits.apply_check_in_reward(
            db,
            user_id=user_id,
            amount_micro=reward_micro,
            check_in_date=today,
            streak_day=streak_day,
        )
        db.add(
            CheckIn(
                user_id=user_id,
                check_in_date=today,
                streak_day=streak_day,
                ledger_id=entry.id,
            )
        )
        await db.flush()  # surface the UNIQUE(user_id, date) conflict now
    except (CheckInAlreadyClaimed, IntegrityError):
        # Concurrent claim won the race — either the ledger idempotency key
        # or the check_in UNIQUE tripped. ``apply_check_in_reward`` already
        # rolled back on the former; the latter needs it here.
        await db.rollback()
        raise AlreadyCheckedInError("Already checked in today")

    logger.info(
        "check-in claimed: user=%s date=%s streak=%s",
        user_id,
        today,
        streak_day,
    )
    return ClaimedCheckIn(reward_micro=reward_micro, streak_day=streak_day)


class CheckInRankOrder(StrEnum):
    """How to rank the same per-user check-in aggregate.

    Two questions, one query — the same shape ``credits.analytics`` uses
    for spend: *who checks in most* (habit) and *who checked in last*
    (recency) differ only in ``ORDER BY``, so they cannot drift into two
    definitions of a check-in.
    """

    COUNT = "count"
    RECENT = "recent"


_RANK_ORDER_BY = {
    CheckInRankOrder.COUNT: "check_ins DESC, last_check_in_at DESC",
    CheckInRankOrder.RECENT: "last_check_in_at DESC",
}


@dataclass(frozen=True, slots=True)
class CheckInActivity:
    """One user's check-in habit over the window, for either ranking."""

    user_id: int
    username: str
    name: str
    check_ins: int
    streak_day: int
    last_check_in_at: datetime


async def user_activity(
    db: AsyncSession,
    *,
    since: datetime,
    limit: int,
    order: CheckInRankOrder,
) -> list[CheckInActivity]:
    """Per-user check-ins over the window, ranked by ``order``.

    ``streak_day`` is read off the **latest** row rather than aggregated:
    it is a running count the claim itself computes, so the maximum in a
    window would report a streak the user has since broken, and a sum
    would be meaningless. ``COUNT`` ties break on recency, so a leaderboard
    of equal counts still puts the currently-active user first.
    """
    rows = (
        await db.execute(
            text(f"""
            SELECT
                c.user_id,
                u.username,
                u.name,
                COUNT(*) AS check_ins,
                MAX(c.created_at) AS last_check_in_at,
                (ARRAY_AGG(c.streak_day ORDER BY c.check_in_date DESC))[1]
                    AS streak_day
            FROM check_in c
            JOIN "user" u ON u.id = c.user_id
            WHERE c.created_at >= :since
            GROUP BY c.user_id, u.username, u.name
            ORDER BY {_RANK_ORDER_BY[order]}
            LIMIT :limit
            """),
            {"since": since, "limit": limit},
        )
    ).all()
    return [
        CheckInActivity(
            user_id=row.user_id,
            username=row.username,
            name=row.name,
            check_ins=int(row.check_ins),
            streak_day=int(row.streak_day),
            last_check_in_at=row.last_check_in_at,
        )
        for row in rows
    ]


async def count_in_window(db: AsyncSession, *, since: datetime) -> tuple[int, int]:
    """``(check_ins, distinct users)`` claimed since ``since``.

    Lives here rather than with the credit reporting because the ledger
    does not know what a check-in is: it sees only ``CHECKIN`` grant
    rows, which the daily reward could stop producing without the habit
    itself changing. ``check_in`` is the record of the habit.
    """
    row = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count(func.distinct(CheckIn.user_id)).label("users"),
            )
            .select_from(CheckIn)
            .where(CheckIn.created_at >= since)
        )
    ).one()
    return int(row.total), int(row.users)
