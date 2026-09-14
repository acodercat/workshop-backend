"""Operator-account management — the console's own directory.

- ``GET    /staff``                 — list accounts.
- ``POST   /staff``                 — provision one.
- ``GET    /staff/{id}``            — one account.
- ``PATCH  /staff/{id}``            — name / status.
- ``PUT    /staff/{id}/password``   — reset someone's password.

There is no role tier — every signed-in operator can read and manage the
directory (see ``docs/admin-console.md``); the operator team is small and
named, so there are no gates between operators.

**This module is the reference slice.** Every future console feature —
credit adjustments, withdrawal review, invite issuance — follows its
shape: ``StaffContext`` in the signature, mutations ending in their own
``await ctx.db.commit()``, invariants in the service layer, plain reads
inline.
"""

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import func, select

from app.api.admin.dependencies import StaffContext
from app.common.pagination import paginate
from app.errors import StaffNotFoundError
from app.models.staff import Staff
from app.schemas.admin.staff import (
    StaffCreate,
    StaffPasswordReset,
    StaffResponse,
    StaffUpdate,
)
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.services.admin import AdminContext, staff as staff_service

router = APIRouter(tags=["staff"])


async def _get_or_404(ctx: AdminContext, staff_id: int) -> Staff:
    """Load an account or raise 404.

    No ownership dimension to fold in, unlike the customer-facing
    ``_get_or_404`` helpers: any signed-in operator may read any account,
    so this is a plain lookup.
    """
    account = await ctx.db.get(Staff, staff_id)
    if account is None:
        raise StaffNotFoundError(f"No staff account with id {staff_id}.")
    return account


@router.get("/staff", response_model=PaginatedResponse[StaffResponse])
async def list_staff(
    ctx: StaffContext,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[StaffResponse]:
    """Return one page of the operator directory, ordered by username.

    Alphabetical rather than newest-first: this is a directory someone
    scans for a name, not a feed.
    """
    total = await ctx.db.scalar(select(func.count()).select_from(Staff))
    rows = (
        await ctx.db.scalars(paginate(select(Staff).order_by(Staff.username), page, page_size))
    ).all()
    return PaginatedResponse(
        items=[StaffResponse.model_validate(row) for row in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.post("/staff", response_model=StaffResponse, status_code=status.HTTP_201_CREATED)
async def create_staff(ctx: StaffContext, body: StaffCreate) -> StaffResponse:
    """Provision an operator account."""
    account = await staff_service.create(
        ctx.db,
        username=body.username,
        name=body.name,
        password=body.password,
    )
    await ctx.db.commit()

    return StaffResponse.model_validate(account)


@router.get("/staff/{staff_id}", response_model=StaffResponse)
async def read_staff(ctx: StaffContext, staff_id: int) -> StaffResponse:
    """Return one operator account."""
    return StaffResponse.model_validate(await _get_or_404(ctx, staff_id))


@router.patch("/staff/{staff_id}", response_model=StaffResponse)
async def update_staff(ctx: StaffContext, staff_id: int, body: StaffUpdate) -> StaffResponse:
    """Change an account's name or status.

    A status change that would leave no active account is refused by the
    service layer, and the transaction rolls back with it.
    """
    account = await _get_or_404(ctx, staff_id)

    await staff_service.update(ctx.db, account, name=body.name, status=body.status)
    await ctx.db.commit()

    return StaffResponse.model_validate(account)


@router.put("/staff/{staff_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_staff_password(
    ctx: StaffContext, staff_id: int, body: StaffPasswordReset
) -> Response:
    """Set another operator's password, signing that account out everywhere.

    The new password is never recorded anywhere — this deployment keeps
    no operator trail at all.
    """
    account = await _get_or_404(ctx, staff_id)

    await staff_service.set_password(ctx.db, account, body.password)
    await ctx.db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)
