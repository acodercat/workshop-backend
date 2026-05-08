"""User endpoints — own profile, password change, per-user SSE feed.

Admin user-listing was removed: the frontend never called it, and
keeping unused endpoints invites scope creep and security regressions
(forgotten auth, slow queries, etc.). Re-add when there is an actual
admin UI, ideally with a dedicated search index instead of ``ilike``.
"""

from fastapi import APIRouter, Depends, Header, Request, status
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import get_db, get_redis, get_redis_streaming
from app.auth import (
    authenticate_and_get_user,
    authenticate_for_streaming,
    get_password_hash,
    revoke_user_tokens,
    verify_password,
)
from app.errors import IncorrectPasswordError, SamePasswordError
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.user import (
    EmailChangeCodeRequest,
    EmailChangeRequest,
    PasswordChange,
    ProfileUpdate,
    UserResponse,
)
from app.services.auth.email_change import (
    change_email,
    request_email_change_code,
)
from app.services.user import stream_user_events
from config import settings

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def get_current_user(
    current_user: User = Depends(authenticate_and_get_user),
):
    return current_user


@router.patch("/me", response_model=UserResponse)
@limiter.limit("10/minute")
async def update_profile(
    request: Request,
    data: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Partial update of the caller's own profile.

    All fields in :class:`ProfileUpdate` are optional. ``model_dump
    (exclude_unset=True)`` distinguishes "field omitted" (leave column
    alone) from "field set to None" (clear the column).

    JWT-only — profile changes don't touch credentials.
    """
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(current_user, field, value)
    await db.commit()
    await db.refresh(current_user)
    return current_user


@router.get("/me/events")
async def stream_current_user_events(
    redis: aioredis.Redis = Depends(get_redis_streaming),
    current_user: User = Depends(authenticate_for_streaming),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """Long-lived SSE stream of cross-session notifications for the
    authenticated user. Receives ``session.status_changed`` events for
    any session this user owns, regardless of which page the client is
    on. The generator runs until the HTTP client disconnects.

    ``Last-Event-ID`` is sent by the browser on reconnect; see
    :func:`stream_user_events` for the replay semantics.

    Auth uses :func:`authenticate_for_streaming` (not the standard
    request-scoped variant) so the JWT lookup's DB session closes before
    the SSE response begins — see that function's docstring for why.
    """
    return EventSourceResponse(
        stream_user_events(
            redis, current_user.id, last_event_id=last_event_id,
        )
    )


@router.put("/me/password", response_model=UserResponse)
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    data: PasswordChange,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    if not await verify_password(data.current_password, current_user.password_hash):
        raise IncorrectPasswordError("Current password incorrect")
    # Refuse no-op changes: a same-password "update" would still bump
    # ``token_version`` and kick every active session out for nothing.
    if await verify_password(data.new_password, current_user.password_hash):
        raise SamePasswordError("New password matches current password")
    current_user.password_hash = await get_password_hash(data.new_password)
    # Bump the token version so previously issued JWTs stop validating.
    revoke_user_tokens(current_user)
    await db.commit()
    await db.refresh(current_user)
    return current_user


# ---- email change ---------------------------------------------------------
#
# Two-step verify-then-commit, mirrors the registration flow:
#
#   POST /me/email/verification-codes  { new_email, current_password, locale }
#       → sudo + uniqueness pre-check + send 6-digit code to new_email. 204.
#   PUT /me/email                      { new_email, code }
#       → verify code, atomically swap user.email, bump token_version.
#
# All failure paths raise typed ``AppError`` subclasses (defined in
# :mod:`app.errors`); ``app_error_handler`` serializes them into the
# ``{error: {type, message}}`` envelope. No translation table needed
# at this layer.


@router.post(
    "/me/email/verification-codes",
    status_code=status.HTTP_204_NO_CONTENT,
)
@limiter.limit(settings.rate_limit_register_code)
async def request_email_change_verification_code(
    request: Request,
    body: EmailChangeCodeRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Step 1: dispatch a verification code to the new email."""
    await request_email_change_code(
        db,
        redis,
        user=current_user,
        new_email=body.new_email,
        current_password=body.current_password,
        locale=body.locale,
    )


@router.put("/me/email", response_model=UserResponse)
@limiter.limit("5/minute")
async def update_email(
    request: Request,
    body: EmailChangeRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Step 2: verify the code and apply the new email."""
    return await change_email(
        db,
        redis,
        user=current_user,
        new_email=body.new_email,
        code=body.code,
    )
