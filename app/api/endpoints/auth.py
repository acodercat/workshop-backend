from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_redis
from app.auth import assert_user_active, authenticate_user, create_access_token
from app.rate_limit import limiter
from app.schemas.auth import (
    AccessTokenResponse,
    EmailLoginRequest,
    LoggedInResponse,
    RegisterCodeRequest,
    RegisterRequest,
)
from app.services.auth import (
    login_by_code,
    register_user,
    request_register_code,
)
from config import settings


router = APIRouter()


@router.post("/auth/access-tokens", response_model=AccessTokenResponse)
@limiter.limit(settings.rate_limit_login)
async def login_for_access_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """Authenticate the user and issue a JWT access token.

    Username + password sign-in. Coexists with the email-OTP flow;
    the frontend's auth dialog opens on the email tab by default
    and offers a "用户名密码登录" link to swap to this flow. Useful
    for accounts without an email on file or users who prefer
    password-based sign-in.
    """
    user = await authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Inactive accounts reuse the wrong-credentials message so a disabled
    # account is indistinguishable from a bad password.
    assert_user_active(user, inactive_detail="Incorrect username or password")

    user.last_login_at = datetime.now(timezone.utc)
    access_token = create_access_token(
        data={"sub": user.username, "token_version": user.token_version},
    )
    await db.commit()

    return AccessTokenResponse(access_token=access_token, token_type="bearer")


#
# Login and registration are separate flows over a shared code
# dispatch: ``POST /auth/verification-codes`` (send) feeds either
# ``POST /auth/access-tokens-by-code`` (login only — peek-verify +
# issue a JWT for an existing ACTIVE user, else ``not_registered``) or
# ``POST /users`` (invite-gated registration — validate invite +
# consume code + create ACTIVE user + issue a JWT).
#
# Registration is invite-gated and a valid invite is the access grant,
# so new users are created ``ACTIVE`` and both endpoints return the
# same ``kind="logged_in"`` response with a token.
#
# All failure paths raise ``AppError`` subclasses (defined in
# :mod:`app.errors`) that bubble up to ``app_error_handler`` —
# ``status_code`` and ``error_type`` live on each subclass, so this
# layer carries no reason → status translation table.


@router.post("/auth/verification-codes", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.rate_limit_register_code)
async def create_verification_code(
    request: Request,
    body: RegisterCodeRequest,
    redis: aioredis.Redis = Depends(get_redis),
):
    """Dispatch a 6-digit verification code to ``email``.

    Returns 204 regardless of whether the email is already a
    registered user — telling the caller "this address is registered"
    here would leak account existence. The unified flow's branching
    happens at :func:`access_token_by_code` after the code is
    proven; until then, the endpoint is indistinguishable for new
    and returning users.

    ``CooldownError`` / ``EmailSendFailedError`` from
    ``request_register_code`` propagate to ``app_error_handler``.
    """
    await request_register_code(
        redis,
        email=body.email,
        locale=body.locale,
    )


@router.post(
    "/auth/access-tokens-by-code",
    response_model=LoggedInResponse,
)
@limiter.limit(settings.rate_limit_login)
async def access_token_by_code(
    request: Request,
    body: EmailLoginRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Log an existing user in by email + verification code.

    **Login only** — peek-verifies the code, then:

    - Email maps to an existing ``ACTIVE`` user → consume the code
      now (we're committing to log them in), issue a JWT.
    - Email isn't registered → ``NotRegisteredError`` (404); the
      frontend steers the user to the invite-gated register tab.
    - Email maps to ``DISABLED`` / ``SUSPENDED`` → 401 opaque, same
      as :func:`login_for_access_token`.
    """
    return await login_by_code(db, redis, email=body.email, code=body.code)


@router.post(
    "/users",
    response_model=LoggedInResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(settings.rate_limit_register)
async def create_user(
    request: Request,
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Invite-gated registration via the email-OTP flow.

    Validates the invite code, consumes the verification code,
    generates the ``axon_${id}`` username + a random password
    placeholder, INSERTs the user ``ACTIVE``, mints their personal
    referral code, records the redemption, and issues a JWT — a valid
    invite is the access grant, so the response logs the user straight
    in (``kind="logged_in"``).

    ``register_user`` raises typed ``AppError`` subclasses
    (``InvalidInviteCodeError``, ``InviteExpiredError``,
    ``InviteExhaustedError``, ``EmailTakenError``,
    ``InvalidVerificationCodeError``, ``TooManyVerificationAttemptsError``)
    on any failure path; they bubble straight up to
    ``app_error_handler``.
    """
    return await register_user(
        db,
        redis,
        email=body.email,
        code=body.code,
        invite_code=body.invite_code,
        name=body.name,
    )
