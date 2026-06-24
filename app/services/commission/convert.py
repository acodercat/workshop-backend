"""Convert commission cash into spendable credits.

The one place two ledgers move together: it debits the commission wallet
(a ``CONVERT`` row, negative 分) and tops up the credit balance (a
``TOPUP`` row on ``credit_ledger`` via ``credits.apply_topup``), in a
single transaction. Financially this turns a cash-payable liability into
a prepaid-service liability at the customer-facing sale price
(``credit_price_cny``, ¥0.1/credit) — the same rate a user gets when
recharging, so converting is fair value and it reduces real cash payout.

Like the rest of the package, takes an ``AsyncSession`` and **does not
commit** — the caller (endpoint) owns the transaction.
"""

import logging
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import (
    CommissionAmountRangeError,
    CommissionDisabledError,
    InsufficientCommissionError,
)
from app.models.commission_ledger import CommissionLedgerKind
from app.services import credits
from app.services.commission._internal import insert_ledger_and_bump_balance
from app.services.commission.balance import lock_withdrawable_fen
from app.services.credits.rates import micro_from_credits
from config import settings

logger = logging.getLogger(__name__)


def _fen_per_credit() -> int:
    """RMB cents that buy one credit at the sale-price peg
    (``credit_price_cny``; ¥0.1 → 10 分). ``Decimal`` so 0.1 doesn't drift;
    the peg is whole-分 by configuration."""
    return int((Decimal(str(settings.credit_price_cny)) * 100).to_integral_value())


class ConvertResult(NamedTuple):
    """Outcome of a commission→credits conversion."""

    spent_fen: int
    granted_credits: int


async def convert_to_credits(
    db: AsyncSession, *, user_id: int, amount_fen: int,
) -> ConvertResult:
    """Convert ``amount_fen`` of withdrawable commission into credits.

    Raises :class:`CommissionDisabledError` if the feature is off,
    :class:`CommissionAmountRangeError` below the minimum (or if the
    amount is too small to buy a whole credit), or
    :class:`InsufficientCommissionError` if the withdrawable balance
    can't cover it. The caller commits.
    """
    if not settings.commission_enabled:
        raise CommissionDisabledError("Commission conversion is not enabled")
    if amount_fen < settings.commission_min_convert_fen:
        raise CommissionAmountRangeError(
            f"Conversion below the ¥{settings.commission_min_convert_fen / 100:.2f} minimum"
        )

    # Conservation-exact: convert only the part that maps to whole credits
    # and debit exactly that. The sub-credit remainder (amount_fen %
    # fen_per_credit) stays in the wallet, so cash-out == credits-in at the
    # peg — an internal wallet→wallet transfer must not leak value. All
    # integer 分 math (no float hop).
    fen_per_credit = _fen_per_credit()
    granted_credits = amount_fen // fen_per_credit
    spent_fen = granted_credits * fen_per_credit
    if granted_credits <= 0:
        raise CommissionAmountRangeError(
            "Conversion amount is too small to buy a whole credit"
        )

    withdrawable_fen = await lock_withdrawable_fen(db, user_id)
    if amount_fen > withdrawable_fen:
        raise InsufficientCommissionError(
            "Conversion exceeds the withdrawable commission balance"
        )

    # Debit commission first so the CONVERT row's id back-links the paired
    # credit TOPUP, and the topup's idempotency key is derived from it.
    entry = await insert_ledger_and_bump_balance(
        db,
        user_id=user_id,
        amount_fen=-spent_fen,
        kind=CommissionLedgerKind.CONVERT,
        reference={"granted_credits": granted_credits},
    )
    ledger = await credits.apply_topup(
        db,
        user_id=user_id,
        amount_micro=micro_from_credits(granted_credits),
        idempotency_key=f"commission-convert:{entry.id}",
        reason="commission conversion",
    )
    entry.reference = {**entry.reference, "credit_ledger_id": ledger.id}
    logger.info(
        "commission converted: user=%s spent_fen=%s granted_credits=%s",
        user_id, spent_fen, granted_credits,
    )
    return ConvertResult(spent_fen=spent_fen, granted_credits=granted_credits)
