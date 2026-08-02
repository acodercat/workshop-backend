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
from datetime import date, datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AlreadyCheckedInError, CheckInDisabledError
from app.models.check_in import CheckIn
from app.services import credits
from app.services.credits.check_in import CheckInAlreadyClaimed
from app.services.credits.rates import micro_from_credits
from config import settings

logger = logging.getLogger(__name__)


class CheckInStatus(NamedTuple):
    """Snapshot for ``GET /check-ins/status``. ``streak_day`` is the
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


def _business_today() -> date:
    """Today's calendar date in the configured business timezone."""
    return datetime.now(ZoneInfo(settings.checkin_timezone)).date()


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
    today = _business_today()
    latest = await _latest_check_in(db, user_id)
    total_days = await db.scalar(
        select(func.count(CheckIn.id)).where(CheckIn.user_id == user_id)
    ) or 0
    return CheckInStatus(
        enabled=settings.checkin_reward_credits > 0,
        checked_in_today=latest is not None and latest.check_in_date == today,
        streak_day=_current_streak(latest, today),
        total_days=total_days,
        reward_micro=micro_from_credits(settings.checkin_reward_credits),
    )


async def claim_daily_check_in(
    db: AsyncSession, *, user_id: int,
) -> ClaimedCheckIn:
    """Claim today's check-in: grant the reward + record the ``check_in``
    row, atomic. The caller commits.

    Raises :class:`CheckInDisabledError` if the feature is off, or
    :class:`AlreadyCheckedInError` if today is already claimed (fast path
    on the pre-read, and the DB double-guard on a concurrent claim).
    """
    if settings.checkin_reward_credits <= 0:
        raise CheckInDisabledError("Daily check-in is not enabled")

    today = _business_today()
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
        "check-in claimed: user=%s date=%s streak=%s", user_id, today, streak_day,
    )
    return ClaimedCheckIn(reward_micro=reward_micro, streak_day=streak_day)
