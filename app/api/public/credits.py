"""Credits read endpoints.

Two surfaces (Stripe-style resource separation):

- ``GET /credits/balance`` — just the spendable balance. Lean;
  drives the sidebar pill and settings balance card.
- ``GET /credits/transactions`` — pure paginated user-facing
  transactions (turn-related 1-3 ledger rows collapsed into one),
  same shape as ``GET /sessions``. Drives the settings Activity tab.

Both lazily sweep open reservations before reading so a just-
finished turn is reflected (settled balance / non-pending rows).
Sweep cost is bounded by the ``state='OPEN'`` partial index —
typically 0 or 1 row per request.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.models.user import User
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.credits import (
    CreditsBalanceSnapshotResponse,
    CreditsTransactionEntry,
)
from app.services import credits
from app.services.credits.rates import credits_from_micro

router = APIRouter()


@router.get(
    "/credits/balance",
    response_model=CreditsBalanceSnapshotResponse,
)
async def get_credits_balance(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> CreditsBalanceSnapshotResponse:
    """Return just ``current_user``'s spendable balance.

    Stripe's ``GET /v1/balance`` analog — a lean snapshot, separate
    from the transactions list, so each surface is a clean single
    resource. Lazy-sweeps any of this user's open reservations whose
    session is already terminal so the number reflects fully-closed
    turns.
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
    ``GET /credits/balance``). Lazy-sweeps open reservations so a
    just-finished turn shows as settled rather than pending.
    """
    await credits.settle_open_for_user(db, current_user.id)
    rows, total = await credits.list_transactions(
        db,
        user_id=current_user.id,
        page=page,
        page_size=page_size,
    )
    await db.commit()  # the sweep's writes need to land

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
