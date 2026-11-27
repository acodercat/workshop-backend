"""The console's user directory — profile, credits, sessions.

- ``GET   /users``                              — search / list.
- ``GET   /users/{id}``                         — profile + balance + session count.
- ``PATCH /users/{id}``                         — account status.
- ``GET   /users/{id}/credits/transactions``    — the user's ledger, as they see it.
- ``POST  /users/{id}/credits/adjustments``     — manual signed correction.
- ``GET   /users/{id}/sessions``                — session list, soft-deleted included.

Deliberately absent: email edits (the sudo-gated self-serve flow
owns those), password resets (OTP owns recovery), user creation (the
invite flow owns it), deletion, impersonation.
"""

import sqlalchemy as sa
from fastapi import APIRouter, Query, status
from sqlalchemy import func, select

from app.api.admin.dependencies import StaffContext
from app.api.admin.session_rows import session_responses
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
from app.services.admin import AdminContext
from app.services.credits.rates import credits_from_micro, micro_from_credits

router = APIRouter(tags=["users"])

# ``apply_adjust`` still takes a real justification because
# ``scripts/adjust_credits.py`` passes an operator-typed ``--reason``.
# The console stopped asking for one, so it names itself instead; who
# made the correction is already on the same ledger reference as
# ``admin_id``.
_ADJUST_REASON = "admin console adjustment"


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
        status=user.status,
        balance_credits=credits_from_micro(user.credit_balance),
        created_at=user.created_at,
    )


@router.get("/users", response_model=PaginatedResponse[UserResponse])
async def list_users(
    ctx: StaffContext,
    q: str | None = Query(
        None, min_length=1, max_length=64, description="Matches username or email"
    ),
    status_: UserStatus | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[UserResponse]:
    """Return one page of the user directory, newest first.

    One ``q`` searched across the two identifiers a support ticket
    arrives with — this deployment signs in by email only. Plain ILIKE — at the current user count a trigram
    index would be jewellery.
    """
    filters = []
    if q is not None:
        pattern = f"%{q}%"
        filters.append(
            sa.or_(
                User.username.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    if status_ is not None:
        filters.append(User.status == status_)

    total = await ctx.db.scalar(select(func.count()).select_from(User).where(*filters))
    rows = (
        await ctx.db.scalars(
            paginate(
                select(User).where(*filters).order_by(User.created_at.desc(), User.id.desc()),
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

    A no-op (status already there) returns the user without writing.
    """
    user = await _get_or_404(ctx, user_id)
    if user.status == body.status:
        return _to_user_response(user)

    user.status = body.status
    await ctx.db.commit()

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
    row's reference. The idempotency key is namespaced ``admin:`` so a
    console key can never collide with another producer's key in the
    ledger's UNIQUE column.
    """
    user = await _get_or_404(ctx, user_id)

    try:
        entry = await credits.apply_adjust(
            ctx.db,
            user_id=user.id,
            amount_micro=micro_from_credits(body.amount_credits),
            idempotency_key=f"admin:{body.idempotency_key}",
            reason=_ADJUST_REASON,
            admin_id=ctx.staff.id,
        )
        balance_micro = await credits.get_balance(ctx.db, user.id)
        await ctx.db.commit()
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
                select(Session)
                .where(filter_clause)
                # Same tiebreak as the platform-wide list, for the same
                # reason: OFFSET paging over a sort free to reorder equal
                # keys drops or repeats rows between pages.
                .order_by(Session.updated_at.desc(), Session.id.desc()),
                page,
                page_size,
            )
        )
    ).all()

    return PaginatedResponse(
        items=await session_responses(ctx.db, rows),
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )
