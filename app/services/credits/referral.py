"""Referral reward. The only entry point that creates ``REFERRAL``
ledger rows — credit granted to a referrer when an invitee registers
via their referral code."""

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_ledger import CreditLedger, CreditLedgerKind
from app.services.credits._internal import insert_ledger_and_bump_balance

logger = logging.getLogger(__name__)


class ReferralRewardAlreadyApplied(Exception):
    """Raised when a referral reward with the same ``idempotency_key``
    already exists. Callers treat it as success — the prior call moved
    the referrer's balance, so a retry is a no-op."""


async def apply_referral_reward(
    db: AsyncSession,
    *,
    referrer_user_id: int,
    invitee_user_id: int,
    amount_micro: int,
    idempotency_key: str,
) -> CreditLedger:
    """Insert a ``REFERRAL`` row + bump the referrer's balance, atomic
    under the referrer's user lock.

    Does **not** commit on success — the caller owns the transaction.
    Mirrors :func:`app.services.credits.topup.apply_topup`: on a
    duplicate ``idempotency_key`` it rolls the session back itself
    (Postgres aborts the tx on the UNIQUE violation) before raising
    :class:`ReferralRewardAlreadyApplied`, so the caller can resume.
    """
    if amount_micro <= 0:
        raise ValueError("Referral reward must be positive")

    try:
        return await insert_ledger_and_bump_balance(
            db,
            user_id=referrer_user_id,
            amount=amount_micro,
            kind=CreditLedgerKind.REFERRAL,
            reference={"invitee_user_id": invitee_user_id, "source": "referral"},
            idempotency_key=idempotency_key,
        )
    except IntegrityError as exc:
        await db.rollback()
        logger.info(
            "referral reward duplicate idempotency_key=%s referrer=%s (treated as success)",
            idempotency_key, referrer_user_id,
        )
        raise ReferralRewardAlreadyApplied(
            f"Referral reward with idempotency_key={idempotency_key!r} already applied"
        ) from exc
