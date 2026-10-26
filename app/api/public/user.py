"""User endpoints — own profile, password change, per-user SSE feed.

Admin user-listing was removed: the frontend never called it, and
keeping unused endpoints invites scope creep and security regressions
(forgotten auth, slow queries, etc.). Re-add when there is an actual
admin UI, ideally with a dedicated search index instead of ``ilike``.
"""

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import get_db, get_redis, get_user_event_hub
from app.auth import (
    authenticate_and_get_user,
    authenticate_for_streaming,
    get_password_hash,
    revoke_user_tokens,
    verify_password,
)
from app.errors import SamePasswordError
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.user import (
    EmailChangeCodeRequest,
    EmailChangeRequest,
    PasswordChange,
    PhoneChangeCodeRequest,
    PhoneChangeRequest,
    ProfileUpdate,
    ReferralResponse,
    SudoCodeRequest,
    UserResponse,
)
from app.services.auth import credential_change, get_or_create_referral_code
from app.services.auth.sudo import request_sudo_code, require_sudo
from app.services.user import UserEventHub, stream_user_events
from config import settings

router = APIRouter()


@router.get("/me")
async def get_current_user(
    current_user: User = Depends(authenticate_and_get_user),
) -> UserResponse:
    return UserResponse.model_validate(current_user)


@router.get("/me/referral")
async def get_referral(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ReferralResponse:
    """The caller's personal referral code + the per-referral reward.

    New users get a code at registration; this lazily mints one (and
    commits) for accounts that predate the feature.
    """
    code = await get_or_create_referral_code(db, user_id=current_user.id)
    await db.commit()
    return ReferralResponse(
        code=code.code,
        reward_credits=settings.invite_referral_reward_credits,
        invited_count=code.used_count,
    )


@router.patch("/me")
@limiter.limit("10/minute")
async def update_profile(
    request: Request,
    data: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> UserResponse:
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
    return UserResponse.model_validate(current_user)


@router.get("/me/events")
async def stream_current_user_events(
    hub: UserEventHub = Depends(get_user_event_hub),
    current_user: User = Depends(authenticate_for_streaming),
):
    """Long-lived SSE stream of cross-session notifications for the
    authenticated user. Receives ``session.status_changed`` events for
    any session this user owns, regardless of which page the client is
    on. The generator runs until the HTTP client disconnects.

    Live-only: events are fanned out in-process from the per-user
    notification channel (see :class:`~app.services.user.hub.UserEventHub`);
    the page's initial sidebar state comes from ``GET /sessions``, so
    there is no ``Last-Event-ID`` replay.

    Auth uses :func:`authenticate_for_streaming` (not the standard
    request-scoped variant) so the JWT lookup's DB session closes before
    the SSE response begins — see that function's docstring for why.
    """
    return EventSourceResponse(stream_user_events(hub, current_user.id))


#
# Every credential change below is gated on a ``SudoProof`` — see
# :mod:`app.services.auth.sudo`. The endpoint right below issues its code.


@router.post("/me/verification-codes", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.rate_limit_register_code)
async def request_sudo_verification_code(
    request: Request,
    body: SudoCodeRequest,
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Send a sudo code to the caller's own email or phone."""
    await request_sudo_code(
        redis,
        user=current_user,
        channel=body.channel,
        locale=body.locale,
    )


@router.put("/me/password")
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    data: PasswordChange,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
) -> UserResponse:
    await require_sudo(redis, user=current_user, proof=data)
    # Refuse no-op changes: a same-password "update" would still bump
    # ``token_version`` and kick every active session out for nothing.
    if await verify_password(data.new_password, current_user.password_hash):
        raise SamePasswordError("New password matches current password")
    current_user.password_hash = await get_password_hash(data.new_password)
    # Bump the token version so previously issued JWTs stop validating.
    revoke_user_tokens(current_user)
    await db.commit()
    await db.refresh(current_user)
    return UserResponse.model_validate(current_user)


#
# Two-step verify-then-commit, mirrors the registration flow:
#
#   POST /me/email/verification-codes  { new_email, <SudoProof>, locale }
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
    await credential_change.request_change_code(
        db,
        redis,
        user=current_user,
        channel="email",
        new_value=body.new_email,
        proof=body,
        locale=body.locale,
    )


@router.put("/me/email")
@limiter.limit("5/minute")
async def update_email(
    request: Request,
    body: EmailChangeRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
) -> UserResponse:
    """Step 2: verify the code and apply the new email."""
    return UserResponse.model_validate(
        await credential_change.change(
            db,
            redis,
            user=current_user,
            channel="email",
            new_value=body.new_email,
            code=body.code,
        )
    )


#
# Change phone — the SMS-channel mirror of the email-change flow above:
#
#   POST /me/phone/verification-codes  { new_phone, <SudoProof> }
#       → sudo + uniqueness pre-check + send 6-digit SMS code to new_phone. 204.
#   PUT /me/phone                      { new_phone, code }
#       → verify code, atomically swap user.phone, bump token_version.
#


@router.post(
    "/me/phone/verification-codes",
    status_code=status.HTTP_204_NO_CONTENT,
)
@limiter.limit(settings.rate_limit_register_code)
async def request_phone_change_verification_code(
    request: Request,
    body: PhoneChangeCodeRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Step 1: dispatch a verification code to the new phone."""
    await credential_change.request_change_code(
        db,
        redis,
        user=current_user,
        channel="phone",
        new_value=body.new_phone,
        proof=body,
    )


@router.put("/me/phone")
@limiter.limit("5/minute")
async def update_phone(
    request: Request,
    body: PhoneChangeRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
) -> UserResponse:
    """Step 2: verify the code and apply the new phone."""
    return UserResponse.model_validate(
        await credential_change.change(
            db,
            redis,
            user=current_user,
            channel="phone",
            new_value=body.new_phone,
            code=body.code,
        )
    )
