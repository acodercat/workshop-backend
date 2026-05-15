"""Credits read endpoints.

Two surfaces:

- ``GET /credits`` — balance + raw ledger rows. Audit / debug.
- ``GET /credits/transactions`` — balance + user-facing transactions
  (turn-related 1-3 ledger rows collapsed into one). Drives the
  settings UI and sidebar badge.

Both endpoints lazily sweep open reservations before reading so the
returned balance reflects fully-closed turns. Sweep cost is bounded
by the ``state='OPEN'`` partial index — typically 0 or 1 row per
request.
"""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.models.user import User
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.credits import (
    CreditsBalanceResponse,
    CreditsBalanceSnapshotResponse,
    CreditsHistoryEntry,
    CreditsTransactionEntry,
)
from app.services import credits
from app.services.credits.rates import credits_from_micro

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/credits", response_model=CreditsBalanceResponse)
async def get_credits(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> CreditsBalanceResponse:
    """Return ``current_user``'s balance + raw ledger page.

    Sweep-then-read: settle stale reservations first so the balance
    reported here reflects fully-closed turns. The sweep happens in
    the same request transaction, so a failure rolls everything back
    and surfaces — better than silently serving a stale balance.
    """
    await credits.settle_open_for_user(db, current_user.id)
    balance_micro = await credits.get_balance(db, current_user.id)
    rows, total = await credits.list_history(
        db, user_id=current_user.id, page=page, page_size=page_size,
    )
    await db.commit()  # the sweep's writes need to land

    history = [
        CreditsHistoryEntry(
            id=row.id,
            amount_credits=credits_from_micro(row.amount),
            kind=row.kind,
            reference=row.reference,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return CreditsBalanceResponse(
        balance_credits=credits_from_micro(balance_micro),
        history=history,
        history_total=total,
        history_page=page,
        history_page_size=page_size,
    )


@router.get(
    "/credits/balance", response_model=CreditsBalanceSnapshotResponse,
)
async def get_credits_balance(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> CreditsBalanceSnapshotResponse:
    """Return just ``current_user``'s spendable balance.

    Stripe's ``GET /v1/balance`` analog — a lean snapshot, separate
    from the transactions list, so the sidebar badge can poll it
    cheaply. Same lazy-sweep as ``GET /credits``: any of this user's
    open reservations whose session is already terminal get settled
    first, so the number reflects fully-closed turns.
    """
    await credits.settle_open_for_user(db, current_user.id)
    balance_micro = await credits.get_balance(db, current_user.id)
    await db.commit()  # the sweep's writes need to land
    return CreditsBalanceSnapshotResponse(
        balance_credits=credits_from_micro(balance_micro),
    )


@router.get(
    "/credits/transactions",
    response_model=PaginatedResponse[CreditsTransactionEntry],
)
async def get_credits_transactions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[CreditsTransactionEntry]:
    """Return an offset-paginated page of ``current_user``'s
    user-facing transactions.

    Pure ``PaginatedResponse`` — identical shape to ``GET /sessions``
    (``items`` + ``meta``), no balance mixed in (that's
    ``GET /credits/balance``). Same lazy-sweep as ``GET /credits`` so
    a just-finished turn shows as settled rather than pending.
    """
    await credits.settle_open_for_user(db, current_user.id)
    rows, total = await credits.list_transactions(
        db, user_id=current_user.id, page=page, page_size=page_size,
    )
    await db.commit()

    return PaginatedResponse(
        items=[
            CreditsTransactionEntry(
                kind=row.kind,
                amount_credits=credits_from_micro(row.amount),
                at=row.at,
                ref=row.ref,
            )
            for row in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )
