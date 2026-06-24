"""Pydantic schemas for the referral-commission endpoints.

Money on the wire is **yuan** (``*_cny``), matching the buy-side payment
API; the wallet stores RMB cents internally and converts at the endpoint
boundary. ``kind`` on a transaction is the same UPPERCASE identifier the
service layer uses, so the frontend matches stable codes and resolves
localized labels from its own i18n catalog. Credits (from a conversion)
stay whole-credit integers, same as the credit endpoints.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.commission_ledger import CommissionLedgerKind


class CommissionBalanceResponse(BaseModel):
    """Response of ``GET /commissions/balance`` — the wallet snapshot plus
    the active commission rate (for the "earn N% on referrals" copy) and
    the feature flag (so the UI gates the withdraw/convert surface and the
    cash-commission copy while the feature is dark)."""

    enabled: bool
    total_cny: float
    frozen_cny: float
    withdrawable_cny: float
    rate: float


class CommissionTransactionEntry(BaseModel):
    """One commission ledger row. ``amount_cny`` is signed (positive
    commission-in, negative withdraw/convert/clawback); ``available_at``
    is set on ``COMMISSION`` rows (when the funds unfreeze)."""

    kind: CommissionLedgerKind
    amount_cny: float
    at: datetime
    available_at: datetime | None
    ref: dict[str, Any]


class WithdrawalRequestBody(BaseModel):
    """Body of ``POST /commissions/withdrawals``.

    ``amount_cny`` is bounded server-side against
    ``commission_min_withdraw_fen`` and the withdrawable balance
    (``gt=0`` here is only a sanity floor). ``payout_channel`` picks
    where the operator sends the money."""

    amount_cny: float = Field(gt=0, description="Withdrawal amount in RMB")
    payout_channel: Literal["ALIPAY", "BANK"]
    payout_account: str = Field(min_length=1, max_length=128)
    payout_name: str = Field(min_length=1, max_length=64)


class WithdrawalResponse(BaseModel):
    """A withdrawal request — returned on create and in the list."""

    id: uuid.UUID
    amount_cny: float
    status: str
    payout_channel: str
    payout_account: str
    payout_name: str
    payout_ref: str | None
    reject_reason: str | None
    created_at: datetime
    resolved_at: datetime | None


class ConversionRequestBody(BaseModel):
    """Body of ``POST /commissions/conversions`` — convert commission to
    spendable credits. ``amount_cny`` is bounded server-side against
    ``commission_min_convert_fen`` and the withdrawable balance."""

    amount_cny: float = Field(gt=0, description="Commission to convert, in RMB")


class ConversionResponse(BaseModel):
    """Result of a commission→credits conversion, with the post-conversion
    balances so the client can refresh both wallets in one round-trip."""

    spent_cny: float
    granted_credits: int
    withdrawable_cny: float
    credit_balance_credits: int
