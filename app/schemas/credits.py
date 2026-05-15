"""Pydantic schemas for the credit endpoints.

Wire surface deliberately uses **whole credits** (integer; 1 credit
= $0.001 USD of LLM cost), not µ-credits. The µ-credit storage unit
is an internal precision detail; clients never see it. Conversion
happens at the endpoint, not in this module — the schema documents
the shape the frontend renders.

Two surfaces:

- ``GET /credits`` → :class:`CreditsBalanceResponse` — balance +
  raw ledger rows. Audit / debug surface.
- ``GET /credits/transactions`` → :class:`CreditsTransactionsResponse`
  — balance + user-facing transactions (turn-related rows collapsed
  from their 1-3 raw ledger entries). UI surface.

``kind`` enums are the same identifiers the model / service layer
uses (UPPERCASE strings), so frontend can match on stable codes and
resolve localized labels from its own i18n catalog.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.credit_ledger import CreditLedgerKind
from app.services.credits.transactions import TransactionKind


class CreditsBalanceResponse(BaseModel):
    """Top-level shape of ``GET /credits``.

    ``balance_credits`` is the spendable balance after any open
    reservations — what the user can still reserve right now.

    ``history`` is the newest-first page of ledger rows; clients
    paginate via ``page`` / ``page_size`` query params.
    """

    balance_credits: int
    history: list["CreditsHistoryEntry"]
    history_total: int
    history_page: int
    history_page_size: int


class CreditsHistoryEntry(BaseModel):
    """One ledger row, projected for the user-facing history view."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    amount_credits: int
    kind: CreditLedgerKind
    reference: dict[str, Any]
    created_at: datetime


class CreditsTransactionEntry(BaseModel):
    """One user-facing transaction row (collapsed from 1-3 raw
    ledger rows when turn-related, otherwise pass-through)."""

    kind: TransactionKind
    amount_credits: int
    at: datetime
    ref: dict[str, Any]


class CreditsTransactionsResponse(BaseModel):
    """Top-level shape of ``GET /credits/transactions``.

    ``balance_credits`` ships alongside the page so the sidebar
    badge can populate from a single fetch. ``next_cursor`` is the
    timestamp the client passes back as ``before`` to fetch older
    rows — ``None`` means no more data.
    """

    balance_credits: int
    transactions: list[CreditsTransactionEntry]
    next_cursor: datetime | None


CreditsBalanceResponse.model_rebuild()
