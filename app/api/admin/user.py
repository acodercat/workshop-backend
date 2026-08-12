"""The console's user directory — profile, credits, sessions.

- ``GET   /users``                              — search / list.
- ``GET   /users/{id}``                         — profile + balance + session count.
- ``PATCH /users/{id}``                         — account status.
- ``GET   /users/{id}/credits/transactions``    — the user's ledger, as they see it.
- ``POST  /users/{id}/credits/adjustments``     — manual signed correction.
- ``GET   /users/{id}/sessions``                — session list, soft-deleted included.

Deliberately absent: email/phone edits (the sudo-gated self-serve flows
own those), password resets (OTP owns recovery), user creation (the
invite flow owns it), deletion, impersonation.
"""

import sqlalchemy as sa
from fastapi import APIRouter, Query, status
from sqlalchemy import func, select

from app.api.admin.dependencies import StaffContext
from app.common.pagination import paginate
from app.errors import AdjustmentAlreadyAppliedError, UserNotFoundError
from app.models.session import Session
from app.models.user import User, UserStatus
from app.schemas.admin.session import SessionResponse
from app.schemas.admin.user import (
    CreditAdjustmentCreate,
    CreditAdjustmentResponse,
    UserDetailResponse,
    UserResponse,
    UserUpdate,
)
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.credits import CreditsTransactionEntry
from app.services import credits
from app.services.admin import AdminContext, AuditAction, audit
from app.services.credits.rates import credits_from_micro, micro_from_credits

router = APIRouter(tags=["users"])


async def _get_or_404(ctx: AdminContext, user_id: int) -> User:
    """Load a user or raise 404. A plain PK lookup — the console has no
    ownership dimension to fold in."""
    user = await ctx.db.get(User, user_id)
    if user is None:
        raise UserNotFoundError(f"No user with id {user_id}.")
    return user


def _to_user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        name=user.name,
        email=user.email,
        phone=user.phone,
        status=user.status,
        balance_credits=credits_from_micro(user.credit_balance),
        created_at=user.created_at,
    )


@router.get("/users", response_model=PaginatedResponse[UserResponse])
async def list_users(
    ctx: StaffContext,
    q: str | None = Query(
        None, min_length=1, max_length=64, description="Matches username, email or phone"
    ),
    status_: UserStatus | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[UserResponse]:
    """Return one page of the user directory, newest first.

    One ``q`` searched across the three identifiers a support ticket
    arrives with. Plain ILIKE — at the current user count a trigram
    index would be jewellery.
    """
    filters = []
    if q is not None:
        pattern = f"%{q}%"
        filters.append(
            sa.or_(
                User.username.ilike(pattern),
                User.email.ilike(pattern),
                User.phone.ilike(pattern),
            )
        )
    if status_ is not None:
        filters.append(User.status == status_)

    total = await ctx.db.scalar(select(func.count()).select_from(User).where(*filters))
    rows = (
        await ctx.db.scalars(
            paginate(
                select(User).where(*filters).order_by(User.created_at.desc()),
                page,
                page_size,
            )
        )
    ).all()
    return PaginatedResponse(
        items=[_to_user_response(row) for row in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.get("/users/{user_id}", response_model=UserDetailResponse)
async def read_user(ctx: StaffContext, user_id: int) -> UserDetailResponse:
    """Return one user's profile, balance, and session count."""
    user = await _get_or_404(ctx, user_id)
    session_count = await ctx.db.scalar(
        select(func.count()).select_from(Session).where(Session.user_id == user.id)
    )
    return UserDetailResponse(
        **_to_user_response(user).model_dump(),
        last_login_at=user.last_login_at,
        session_count=session_count,
    )


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(ctx: StaffContext, user_id: int, body: UserUpdate) -> UserResponse:
    """Change a user's account status (ACTIVE / DISABLED / SUSPENDED).

    Takes effect on the user's next request — status is checked per
    request in the token decoder, so no ``token_version`` bump is
    involved. The write is inline, the same way the customer surface
    PATCHes ``session.title``: a single invariant-free column has no
    service funnel to call.

    A no-op (status already there) returns the user without writing an
    audit row — a "changed ACTIVE to ACTIVE" entry is noise in a trail
    whose value is that every row means something.
    """
    user = await _get_or_404(ctx, user_id)
    if user.status == body.status:
        return _to_user_response(user)

    before = user.status
    async with audit(
        ctx,
        action=AuditAction.USER_UPDATE,
        target_type="user",
        target_id=user.id,
        reason=body.reason,
    ) as draft:
        user.status = body.status
        draft.payload = {"status": {"from": before.value, "to": body.status.value}}

    return _to_user_response(user)


@router.get(
    "/users/{user_id}/credits/transactions",
    response_model=PaginatedResponse[CreditsTransactionEntry],
)
async def list_user_credit_transactions(
    ctx: StaffContext,
    user_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[CreditsTransactionEntry]:
    """Return one page of the user's transactions — the same collapsed,
    user-facing view the customer sees, because that is the view a
    customer disputes. No lazy sweep here: the console reads, it does
    not settle on the user's behalf."""
    user = await _get_or_404(ctx, user_id)
    rows, total = await credits.list_transactions(
        ctx.db,
        user_id=user.id,
        page=page,
        page_size=page_size,
    )
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


@router.post(
    "/users/{user_id}/credits/adjustments",
    response_model=CreditAdjustmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_credit_adjustment(
    ctx: StaffContext, user_id: int, body: CreditAdjustmentCreate
) -> CreditAdjustmentResponse:
    """Apply a manual signed correction to the user's balance.

    Goes through :func:`credits.apply_adjust` — the funnel that owns the
    ledger — with the operator recorded as ``admin_id`` on the ledger
    row's reference *and* in the audit trail. The idempotency key is
    namespaced ``admin:`` so a console key can never collide with a
    payment-webhook key (``hf:…``) in the ledger's UNIQUE column.
    """
    user = await _get_or_404(ctx, user_id)

    try:
        async with audit(
            ctx,
            action=AuditAction.CREDIT_ADJUST,
            target_type="user",
            target_id=user.id,
            reason=body.reason,
        ) as draft:
            entry = await credits.apply_adjust(
                ctx.db,
                user_id=user.id,
                amount_micro=micro_from_credits(body.amount_credits),
                idempotency_key=f"admin:{body.idempotency_key}",
                reason=body.reason,
                admin_id=ctx.staff.id,
            )
            draft.payload = {
                "amount_credits": body.amount_credits,
                "ledger_id": entry.id,
            }
            balance_micro = await credits.get_balance(ctx.db, user.id)
    except credits.AdjustmentAlreadyApplied as exc:
        raise AdjustmentAlreadyAppliedError(
            "An adjustment with this idempotency key was already applied."
        ) from exc

    return CreditAdjustmentResponse(
        ledger_id=entry.id,
        amount_credits=body.amount_credits,
        balance_credits=credits_from_micro(balance_micro),
    )


@router.get("/users/{user_id}/sessions", response_model=PaginatedResponse[SessionResponse])
async def list_user_sessions(
    ctx: StaffContext,
    user_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[SessionResponse]:
    """Return one page of the user's sessions, most recently active
    first — **including soft-deleted ones**, ``deleted_at`` visible.
    The customer list hides those; support is often asked about exactly
    them."""
    user = await _get_or_404(ctx, user_id)
    filter_clause = Session.user_id == user.id
    total = await ctx.db.scalar(select(func.count()).select_from(Session).where(filter_clause))
    rows = (
        await ctx.db.scalars(
            paginate(
                select(Session).where(filter_clause).order_by(Session.updated_at.desc()),
                page,
                page_size,
            )
        )
    ).all()
    return PaginatedResponse(
        items=[SessionResponse.model_validate(row) for row in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )
