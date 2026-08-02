"""Daily check-in reward. The only entry point that creates ``CHECKIN``
ledger rows — credit granted to a user for a daily check-in claim."""

import logging
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_ledger import CreditLedger, CreditLedgerKind
from app.services.credits._internal import insert_ledger_and_bump_balance

logger = logging.getLogger(__name__)


class CheckInAlreadyClaimed(Exception):
    """Raised when a ``CHECKIN`` row with the same ``idempotency_key``
    (``checkin:{user_id}:{date}``) already exists — the user already
    claimed today. Callers treat it as already-done and surface a 409."""


async def apply_check_in_reward(
    db: AsyncSession,
    *,
    user_id: int,
    amount_micro: int,
    check_in_date: date,
    streak_day: int,
) -> CreditLedger:
    """Insert a ``CHECKIN`` row + bump the user's balance, atomic under
    the user lock.

    Does **not** commit on success — the caller owns the transaction (it
    also inserts the paired ``check_in`` row). Mirrors
    :func:`app.services.credits.referral.apply_referral_reward`: on a
    duplicate ``idempotency_key`` it rolls the session back itself
    (Postgres aborts the tx on the UNIQUE violation) before raising
    :class:`CheckInAlreadyClaimed`, so the caller can resume.
    """
    if amount_micro <= 0:
        raise ValueError("Check-in reward must be positive")

    try:
        return await insert_ledger_and_bump_balance(
            db,
            user_id=user_id,
            amount=amount_micro,
            kind=CreditLedgerKind.CHECKIN,
            reference={
                "check_in_date": check_in_date.isoformat(),
                "streak_day": streak_day,
                "source": "daily_check_in",
            },
            idempotency_key=f"checkin:{user_id}:{check_in_date.isoformat()}",
        )
    except IntegrityError as exc:
        await db.rollback()
        logger.info(
            "check-in duplicate user=%s date=%s (treated as already-claimed)",
            user_id, check_in_date,
        )
        raise CheckInAlreadyClaimed(
            f"Check-in for user={user_id} date={check_in_date} already claimed"
        ) from exc
