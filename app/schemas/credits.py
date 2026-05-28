"""Pydantic schemas for the credit endpoints.

Wire surface deliberately uses **whole credits** (integer; 1 credit
= $0.001 USD of LLM cost), not µ-credits. The µ-credit storage unit
is an internal precision detail; clients never see it. Conversion
happens at the endpoint, not in this module — the schema documents
the shape the frontend renders.

Two surfaces (Stripe-style resource separation — balance is its
own resource, distinct from the transactions list):

- ``GET /credits/balance`` → :class:`CreditsBalanceSnapshotResponse`
  — just the spendable balance. Lean; drives the sidebar pill and
  settings balance card.
- ``GET /credits/transactions`` → ``PaginatedResponse[CreditsTransactionEntry]``
  — pure paginated user-facing transactions (turn-related rows
  collapsed from their 1-3 raw ledger entries), exactly the same
  shape as ``GET /sessions``. No balance mixed in.

``kind`` enum is the same identifier the service layer uses
(UPPERCASE strings), so frontend can match on stable codes and
resolve localized labels from its own i18n catalog.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.services.credits.transactions import TransactionKind


class CreditsBalanceSnapshotResponse(BaseModel):
    """Top-level shape of ``GET /credits/balance``.

    Just the spendable balance (after the lazy sweep settles any
    open reservations whose session is already terminal). Stripe's
    ``GET /v1/balance`` analog — a point-in-time snapshot, kept
    separate from the transactions list so each endpoint is a clean
    single resource. ``GET /credits/transactions`` returns a pure
    ``PaginatedResponse[CreditsTransactionEntry]``.
    """

    balance_credits: int


class CreditsTransactionEntry(BaseModel):
    """One user-facing transaction row (collapsed from 1-3 raw
    ledger rows when turn-related, otherwise pass-through)."""

    kind: TransactionKind
    amount_credits: int
    at: datetime
    ref: dict[str, Any]
