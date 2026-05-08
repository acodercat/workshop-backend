from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_redis
from app.auth import authenticate_user, create_access_token
from app.errors import AccountPendingError
from app.models.user import User, UserStatus
from app.rate_limit import limiter
from app.schemas.auth import (
    AccessTokenResponse,
    EmailLoginExisting,
    EmailLoginNewUser,
    EmailLoginRequest,
    EmailLoginResponse,
    RegisterCodeRequest,
    RegisterRequest,
    RegisterResponse,
)
from app.services.auth import (
    register_user,
    request_register_code,
    verify_register_code,
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
    if user.status is UserStatus.PENDING:
        raise AccountPendingError("Account is awaiting administrator approval")
    if user.status is not UserStatus.ACTIVE:
        # ``DISABLED`` / ``SUSPENDED`` collapse to the same opaque 401
        # as wrong credentials so admin lockouts don't leak.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user.last_login_at = datetime.now(timezone.utc)
    access_token = create_access_token(
        data={"sub": user.username, "token_version": user.token_version},
    )
    await db.commit()

    return AccessTokenResponse(access_token=access_token, token_type="bearer")


# ---- email-OTP unified login + registration ------------------------------
#
# Single flow that handles both first-time signup and returning-user
# login. The same code triple — ``POST /auth/verification-codes``
# (dispatch) → ``POST /auth/access-tokens-by-code`` (peek-verify +
# branch on existence) → ``POST /users`` (consume + create, only for
# new users) — covers every case.
#
# Why peek-verify in step 2 instead of consume-verify: the frontend
# needs to know whether the email already maps to a user *before*
# committing to either log them in or collect a name. Consuming the
# code in step 2 would leave new users without a way to retry
# step 3 if the name input got interrupted; peek + consume-on-create
# means step 3 is the single transactional pivot, while step 2 is
# idempotent.
#
# All failure paths raise ``AppError`` subclasses (defined in
# :mod:`app.errors`) that bubble up to ``app_error_handler`` —
# ``status_code`` and ``error_type`` live on each subclass, so this
# layer no longer carries a reason → status translation table.
#
# Status default for new users is ``PENDING``: email verification
# proves the address but admin approval is the access gate. Login
# endpoints raise ``AccountPendingError`` so the frontend can show a
# "thanks, awaiting approval" message; ``POST /users`` returns
# ``kind="pending_approval"`` instead of a JWT for the same reason.


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
    response_model=EmailLoginResponse,
)
@limiter.limit(settings.rate_limit_login)
async def access_token_by_code(
    request: Request,
    body: EmailLoginRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Verify the code and either log the user in or signal that
    they need to register.

    Peek-verifies the code (does NOT consume), then branches:

    - Email maps to an existing ``ACTIVE`` user → consume the code
      now (we're committing to log them in), issue a JWT.
    - Email isn't registered → leave the code untouched and tell
      the frontend to collect a display name; the subsequent
      ``POST /users`` is the consume-pivot that turns the verified
      code into a row.
    - Email maps to a ``PENDING`` user → ``AccountPendingError``
      (403). Distinguishable from wrong credentials so the user
      knows the gate is admin approval, not the password.
    - Email maps to ``DISABLED`` / ``SUSPENDED`` → 401 opaque, same
      as :func:`login_for_access_token`.

    Why peek for the new-user case: the user might take 30 seconds
    typing their name. If we consumed here, an interrupt between
    this call and ``POST /users`` would leave them with a verified
    intent but no way to actually finish without a fresh code.
    """
    # Peek mode — checks code validity, increments attempts on
    # mismatch (so brute-force is still rate-limited), but doesn't
    # delete on success.
    await verify_register_code(
        redis, email=body.email, code=body.code, consume=False
    )

    existing = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if existing is None:
        # New user — frontend should now show the "what should we call
        # you?" step and POST /users with the same code.
        return EmailLoginNewUser()

    if existing.status is UserStatus.PENDING:
        raise AccountPendingError("Account is awaiting administrator approval")
    if existing.status is not UserStatus.ACTIVE:
        # ``DISABLED`` / ``SUSPENDED`` collapse to the same opaque 401
        # as wrong code so admin lockouts don't leak.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is not active",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Existing ACTIVE user: now consume the code (commit to logging
    # in) and issue the JWT. If the code was somehow deleted between
    # the peek and now, ``verify_register_code`` raises
    # ``InvalidVerificationCodeError``, which the global handler
    # serializes for the client.
    await verify_register_code(
        redis, email=body.email, code=body.code, consume=True
    )

    existing.last_login_at = datetime.now(timezone.utc)
    access_token = create_access_token(
        data={"sub": existing.username, "token_version": existing.token_version},
    )
    await db.commit()
    await db.refresh(existing)
    return EmailLoginExisting(
        access_token=access_token,
        token_type="bearer",
        user=existing,
    )


@router.post(
    "/users",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(settings.rate_limit_register)
async def create_user(
    request: Request,
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Create a new user via the email-OTP flow — only called when
    :func:`access_token_by_code` told the frontend the email isn't
    registered yet.

    Consumes the verification code, generates the ``axon_${id}``
    username + a random password placeholder, INSERTs the user with
    ``status=PENDING``, and returns ``kind="pending_approval"``. No
    JWT is issued: a PENDING account can't access anything until an
    admin flips it to ``ACTIVE``, so the frontend pivots to a
    "thanks, awaiting approval" screen instead of the workspace.

    ``register_user`` raises typed ``AppError`` subclasses
    (``EmailTakenError``, ``InvalidVerificationCodeError``,
    ``TooManyVerificationAttemptsError``) on any failure path; they
    bubble straight up to ``app_error_handler``.
    """
    await register_user(
        db,
        redis,
        email=body.email,
        code=body.code,
        name=body.name,
    )
    # No ``last_login_at`` update or token issuance — the user is
    # ``PENDING`` and can't sign in yet. ``register_user`` already
    # committed the row.
    return RegisterResponse()
