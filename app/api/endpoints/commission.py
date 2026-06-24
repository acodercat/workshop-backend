"""Referral-commission endpoints (返佣提现).

User-facing read + cash-out surface over the commission wallet:

- ``GET  /commissions/balance``      — wallet snapshot (total / frozen /
  withdrawable) + the active rate.
- ``GET  /commissions/transactions`` — paginated ledger history, same
  shape as ``GET /sessions``.
- ``POST /commissions/withdrawals``  — request a cash payout.
- ``GET  /commissions/withdrawals``  — the caller's withdrawal requests.
- ``POST /commissions/conversions``  — convert commission into credits.

Money on the wire is yuan (``*_cny``); the service layer works in RMB
cents. Operator settlement of withdrawals is out-of-band
(``scripts/process_withdrawals.py``), not an HTTP surface.
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.models.user import User
from app.models.withdrawal_request import WithdrawalPayoutChannel, WithdrawalRequest
from app.rate_limit import limiter
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.commission import (
    CommissionBalanceResponse,
    CommissionTransactionEntry,
    ConversionRequestBody,
    ConversionResponse,
    WithdrawalRequestBody,
    WithdrawalResponse,
)
from app.services import commission, credits
from app.services.commission.rates import cny_from_fen, fen_from_cny
from app.services.credits.rates import credits_from_micro
from config import settings

router = APIRouter()


def _to_withdrawal_response(request: WithdrawalRequest) -> WithdrawalResponse:
    return WithdrawalResponse(
        id=request.id,
        amount_cny=cny_from_fen(request.amount_fen),
        status=request.status.value,
        payout_channel=request.payout_channel.value,
        payout_account=request.payout_account,
        payout_name=request.payout_name,
        payout_ref=request.payout_ref,
        reject_reason=request.reject_reason,
        created_at=request.created_at,
        resolved_at=request.resolved_at,
    )


@router.get("/commissions/balance", response_model=CommissionBalanceResponse)
async def get_commission_balance(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> CommissionBalanceResponse:
    """Return ``current_user``'s commission wallet snapshot + the rate."""
    bal = await commission.get_balance(db, current_user.id)
    return CommissionBalanceResponse(
        enabled=settings.commission_enabled,
        total_cny=cny_from_fen(bal.total_fen),
        frozen_cny=cny_from_fen(bal.frozen_fen),
        withdrawable_cny=cny_from_fen(bal.withdrawable_fen),
        rate=settings.commission_rate,
    )


@router.get(
    "/commissions/transactions",
    response_model=PaginatedResponse[CommissionTransactionEntry],
)
async def get_commission_transactions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[CommissionTransactionEntry]:
    """Return an offset-paginated page of ``current_user``'s commission
    transactions — same ``items`` + ``meta`` shape as ``GET /sessions``."""
    rows, total = await commission.list_transactions(
        db, user_id=current_user.id, page=page, page_size=page_size,
    )
    return PaginatedResponse(
        items=[
            CommissionTransactionEntry(
                kind=row.kind,
                amount_cny=cny_from_fen(row.amount_fen),
                at=row.at,
                available_at=row.available_at,
                ref=row.ref,
            )
            for row in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.post("/commissions/withdrawals", response_model=WithdrawalResponse)
@limiter.limit("10/minute")
async def create_withdrawal(
    request: Request,
    body: WithdrawalRequestBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> WithdrawalResponse:
    """Request a cash withdrawal. Debits the wallet up front; the operator
    settles it out-of-band."""
    withdrawal = await commission.request_withdrawal(
        db,
        user_id=current_user.id,
        amount_fen=fen_from_cny(body.amount_cny),
        payout_channel=WithdrawalPayoutChannel(body.payout_channel),
        payout_account=body.payout_account,
        payout_name=body.payout_name,
    )
    await db.commit()
    await db.refresh(withdrawal)  # populate server-side created_at/updated_at
    return _to_withdrawal_response(withdrawal)


@router.get(
    "/commissions/withdrawals",
    response_model=PaginatedResponse[WithdrawalResponse],
)
async def get_withdrawals(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[WithdrawalResponse]:
    """Return an offset-paginated page of ``current_user``'s withdrawal
    requests (newest first)."""
    rows, total = await commission.list_withdrawals(
        db, user_id=current_user.id, page=page, page_size=page_size,
    )
    return PaginatedResponse(
        items=[_to_withdrawal_response(row) for row in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.post("/commissions/conversions", response_model=ConversionResponse)
@limiter.limit("10/minute")
async def create_conversion(
    request: Request,
    body: ConversionRequestBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ConversionResponse:
    """Convert commission into spendable credits; returns both
    post-conversion balances."""
    result = await commission.convert_to_credits(
        db, user_id=current_user.id, amount_fen=fen_from_cny(body.amount_cny),
    )
    await db.commit()
    bal = await commission.get_balance(db, current_user.id)
    credit_micro = await credits.get_balance(db, current_user.id)
    return ConversionResponse(
        spent_cny=cny_from_fen(result.spent_fen),
        granted_credits=result.granted_credits,
        withdrawable_cny=cny_from_fen(bal.withdrawable_fen),
        credit_balance_credits=credits_from_micro(credit_micro),
    )
