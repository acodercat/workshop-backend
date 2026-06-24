"""Cash-out flow over the commission wallet.

Three operations:

- :func:`request_withdrawal` — user submits a cash-out. Debits the
  wallet up front (a ``WITHDRAW`` ledger row) so the funds can't be
  double-spent across two open requests, and persists a PENDING
  ``withdrawal_request`` the operator will settle.
- :func:`mark_paid` / :func:`reject` — operator settlement (driven by
  ``scripts/process_withdrawals.py``). Paying needs no ledger change
  (the debit already happened at request time); rejecting posts a
  ``WITHDRAW_REFUND`` row that returns the funds.

Every function takes an ``AsyncSession`` and **does not commit** — the
caller (endpoint / script) owns the transaction, matching the
``credits`` / ``payment`` service contract.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.pagination import paginate
from app.errors import (
    CommissionAmountRangeError,
    CommissionDisabledError,
    InsufficientCommissionError,
    WithdrawalNotFoundError,
)
from app.models.commission_ledger import CommissionLedgerKind
from app.models.withdrawal_request import WithdrawalRequest, WithdrawalStatus
from app.services.commission._internal import insert_ledger_and_bump_balance
from app.services.commission.balance import lock_withdrawable_fen
from config import settings

logger = logging.getLogger(__name__)


async def request_withdrawal(
    db: AsyncSession,
    *,
    user_id: int,
    amount_fen: int,
    payout_account: str,
    payout_name: str,
) -> WithdrawalRequest:
    """Create a PENDING withdrawal, debiting the wallet up front.

    Raises :class:`CommissionDisabledError` if the feature is off,
    :class:`CommissionAmountRangeError` below the minimum, or
    :class:`InsufficientCommissionError` if the withdrawable balance
    can't cover it. The caller commits.
    """
    if not settings.commission_enabled:
        raise CommissionDisabledError("Commission withdrawals are not enabled")
    if amount_fen < settings.commission_min_withdraw_fen:
        raise CommissionAmountRangeError(
            f"Withdrawal below the ¥{settings.commission_min_withdraw_fen / 100:.2f} minimum"
        )

    withdrawable_fen = await lock_withdrawable_fen(db, user_id)
    if amount_fen > withdrawable_fen:
        raise InsufficientCommissionError(
            "Withdrawal exceeds the withdrawable commission balance"
        )

    request = WithdrawalRequest(
        user_id=user_id,
        amount_fen=amount_fen,
        status=WithdrawalStatus.PENDING,
        payout_account=payout_account,
        payout_name=payout_name,
    )
    db.add(request)
    await db.flush()  # populate request.id for the ledger back-reference

    entry = await insert_ledger_and_bump_balance(
        db,
        user_id=user_id,
        amount_fen=-amount_fen,
        kind=CommissionLedgerKind.WITHDRAW,
        reference={"withdrawal_id": str(request.id)},
    )
    request.ledger_id = entry.id
    return request


async def _lock_request(
    db: AsyncSession, withdrawal_id: uuid.UUID,
) -> WithdrawalRequest:
    """``SELECT ... FOR UPDATE`` a withdrawal request; 404 if absent."""
    request = await db.scalar(
        select(WithdrawalRequest)
        .where(WithdrawalRequest.id == withdrawal_id)
        .with_for_update()
    )
    if request is None:
        raise WithdrawalNotFoundError(f"Withdrawal {withdrawal_id} not found")
    return request


async def mark_paid(
    db: AsyncSession, *, withdrawal_id: uuid.UUID, payout_ref: str,
) -> WithdrawalRequest:
    """Operator: mark a PENDING withdrawal PAID with its transfer
    reference. No ledger change — the funds were debited at request
    time. Raises ``ValueError`` if it's not PENDING. The caller commits.
    """
    request = await _lock_request(db, withdrawal_id)
    if request.status != WithdrawalStatus.PENDING:
        raise ValueError(
            f"Withdrawal {withdrawal_id} is not PENDING (status={request.status.value})"
        )
    request.status = WithdrawalStatus.PAID
    request.payout_ref = payout_ref
    request.resolved_at = datetime.now(timezone.utc)
    logger.info("withdrawal paid: id=%s payout_ref=%s", withdrawal_id, payout_ref)
    return request


async def reject(
    db: AsyncSession, *, withdrawal_id: uuid.UUID, reason: str,
) -> WithdrawalRequest:
    """Operator: reject a PENDING withdrawal, returning the funds via a
    ``WITHDRAW_REFUND`` row. Raises ``ValueError`` if it's not PENDING.
    The caller commits.
    """
    request = await _lock_request(db, withdrawal_id)
    if request.status != WithdrawalStatus.PENDING:
        raise ValueError(
            f"Withdrawal {withdrawal_id} is not PENDING (status={request.status.value})"
        )
    await insert_ledger_and_bump_balance(
        db,
        user_id=request.user_id,
        amount_fen=request.amount_fen,
        kind=CommissionLedgerKind.WITHDRAW_REFUND,
        reference={"withdrawal_id": str(request.id)},
    )
    request.status = WithdrawalStatus.REJECTED
    request.reject_reason = reason
    request.resolved_at = datetime.now(timezone.utc)
    logger.info("withdrawal rejected: id=%s reason=%s", withdrawal_id, reason)
    return request


async def list_withdrawals(
    db: AsyncSession, *, user_id: int, page: int, page_size: int,
) -> tuple[list[WithdrawalRequest], int]:
    """One newest-first page of ``user_id``'s withdrawal requests + the
    total count. Offset pagination via :func:`app.common.pagination.paginate`,
    with the count + page sharing one filter so the total can't drift.
    """
    filter_clause = WithdrawalRequest.user_id == user_id
    total = (
        await db.execute(
            select(func.count(WithdrawalRequest.id)).where(filter_clause)
        )
    ).scalar() or 0
    base = (
        select(WithdrawalRequest)
        .where(filter_clause)
        .order_by(WithdrawalRequest.created_at.desc())
    )
    rows = (await db.execute(paginate(base, page, page_size))).scalars().all()
    return list(rows), total


async def list_pending(db: AsyncSession, *, limit: int) -> list[WithdrawalRequest]:
    """Oldest-first PENDING withdrawals for the operator batch tool."""
    rows = (
        await db.execute(
            select(WithdrawalRequest)
            .where(WithdrawalRequest.status == WithdrawalStatus.PENDING)
            .order_by(WithdrawalRequest.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)
