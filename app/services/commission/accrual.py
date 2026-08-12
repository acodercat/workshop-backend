"""Referral commission accrual — the only entry point that creates
``COMMISSION`` ledger rows.

Earned when an **invitee** recharges: the inviter (the referrer who
brought them in) gets a withdrawable cash commission, a fraction
(``settings.commission_rate``) of the paid amount, held for
``settings.commission_hold_days`` before it's withdrawable.

Two layers, mirroring how :func:`app.services.credits.apply_referral_reward`
is the primitive and ``register.py`` is the best-effort caller:

- :func:`apply_commission` — the thin primitive (positive ``COMMISSION``
  row + balance bump, idempotent), like ``apply_referral_reward``.
- :func:`accrue_for_order` — the payment-driven orchestration: look up
  the referrer, compute + post the commission, **best-effort in its own
  transaction**. Called by the webhook endpoint and the reconciler
  *after* the buyer's top-up is committed, so a referrer-side failure
  can never undo the buyer's recharge (same posture as ``register.py``'s
  post-commit referral reward).
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.commission_ledger import CommissionLedger, CommissionLedgerKind
from app.models.invite_redemption import InviteRedemption
from app.models.payment_order import PaymentOrder, PaymentOrderStatus
from app.services.commission._internal import insert_ledger_and_bump_balance
from app.services.commission.rates import commission_fen
from config import settings

logger = logging.getLogger(__name__)


class CommissionAlreadyApplied(Exception):
    """Raised when a commission with the same ``idempotency_key`` already
    exists. Callers treat it as success — the prior call moved the
    referrer's balance, so a retry is a no-op."""


def _commission_key(out_trade_no: str) -> str:
    """The accrual idempotency key for a paid order. One commission per
    order, stable across the provider's at-least-once retries and a
    reconcile-after-webhook."""
    return "commission:" + out_trade_no


async def apply_commission(
    db: AsyncSession,
    *,
    referrer_user_id: int,
    invitee_user_id: int,
    payment_order_id: uuid.UUID,
    out_trade_no: str,
    amount_fen: int,
    available_at: datetime,
) -> CommissionLedger:
    """Insert a ``COMMISSION`` row + bump the referrer's balance, atomic
    under the referrer's user lock.

    Does **not** commit on success — the caller owns the transaction.
    Mirrors :func:`app.services.credits.apply_referral_reward`: on a
    duplicate ``idempotency_key`` it rolls the session back itself
    (Postgres aborts the tx on the UNIQUE violation) before raising
    :class:`CommissionAlreadyApplied`, so the caller can resume.
    """
    if amount_fen <= 0:
        raise ValueError("Commission must be positive")

    try:
        return await insert_ledger_and_bump_balance(
            db,
            user_id=referrer_user_id,
            amount_fen=amount_fen,
            kind=CommissionLedgerKind.COMMISSION,
            available_at=available_at,
            reference={
                "invitee_user_id": invitee_user_id,
                "payment_order_id": str(payment_order_id),
                "out_trade_no": out_trade_no,
                "source": "referral_commission",
            },
            idempotency_key=_commission_key(out_trade_no),
        )
    except IntegrityError as exc:
        await db.rollback()
        logger.info(
            "commission duplicate out_trade_no=%s referrer=%s (treated as success)",
            out_trade_no,
            referrer_user_id,
        )
        raise CommissionAlreadyApplied(
            f"Commission for out_trade_no={out_trade_no!r} already applied"
        ) from exc


async def accrue_for_order(db: AsyncSession, *, out_trade_no: str) -> None:
    """Accrue referral commission for a just-PAID order — best-effort.

    Called **after** the order's top-up has been committed, reusing the
    caller's session. Re-loads the order fresh (the caller passes only
    the ``out_trade_no`` primitive, so this is safe to call post-commit
    with no expired-attribute access), and:

    - no-ops when commission is disabled, the order isn't PAID, the
      buyer wasn't referred (SYSTEM code → no referrer), or the computed
      commission floors to zero;
    - otherwise posts the ``COMMISSION`` row and commits its own
      transaction.

    Never raises into the caller: a duplicate is swallowed
    (idempotent), and any other failure is rolled back + logged loudly
    — the buyer's committed recharge must not be affected by a
    referrer-side problem.
    """
    if not settings.commission_enabled:
        return
    try:
        order = await db.scalar(
            select(PaymentOrder).where(PaymentOrder.out_trade_no == out_trade_no)
        )
        if order is None or order.status != PaymentOrderStatus.PAID:
            return

        referrer_user_id = await db.scalar(
            select(InviteRedemption.referrer_user_id).where(
                InviteRedemption.invitee_user_id == order.user_id
            )
        )
        if referrer_user_id is None:
            return  # invited via a SYSTEM code (or not invited) — no referrer

        amount_fen = commission_fen(order.amount_fen, settings.commission_rate)
        if amount_fen <= 0:
            return

        available_at = datetime.now(UTC) + timedelta(days=settings.commission_hold_days)
        await apply_commission(
            db,
            referrer_user_id=referrer_user_id,
            invitee_user_id=order.user_id,
            payment_order_id=order.id,
            out_trade_no=out_trade_no,
            amount_fen=amount_fen,
            available_at=available_at,
        )
        await db.commit()
        logger.info(
            "commission accrued: referrer=%s invitee=%s out_trade_no=%s amount_fen=%s",
            referrer_user_id,
            order.user_id,
            out_trade_no,
            amount_fen,
        )
    except CommissionAlreadyApplied:
        pass  # a prior delivery / reconcile already accrued — no-op
    except Exception:
        await db.rollback()
        logger.exception(
            "commission accrual failed for out_trade_no=%s (top-up committed)",
            out_trade_no,
        )
