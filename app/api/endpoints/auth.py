from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._response import error_response
from app.api.dependencies import get_db, get_redis
from app.auth import authenticate_user, create_access_token
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
    RegisterError,
    VerificationError,
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
        return error_response(
            status_code=status.HTTP_403_FORBIDDEN,
            error="account_pending",
            message="账号正在等待管理员审批",
        )
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
# Errors map to HTTP statuses by ``reason``:
#   cooldown / too_many_attempts                       → 429
#   email_send_failed                                  → 502
#   verification_not_found / verification_mismatch     → 400 (invalid_code)
#   email_taken                                        → 409
#   account_pending                                    → 403 (login attempt
#                                                            on a freshly
#                                                            registered user)
#
# Status default for new users is ``PENDING``: email verification
# proves the address but admin approval is the access gate. Login
# endpoints surface ``account_pending`` so the frontend can show a
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
    """
    try:
        await request_register_code(
            redis,
            email=body.email,
            locale=body.locale,
        )
    except VerificationError as exc:
        if exc.reason == "cooldown":
            return error_response(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                error="cooldown",
                message=exc.message,
            )
        if exc.reason == "email_send_failed":
            return error_response(
                status_code=status.HTTP_502_BAD_GATEWAY,
                error="email_send_failed",
                message=exc.message,
            )
        # Defensive: any other reason from this entry point is a
        # programmer error (verify path returns those reasons), not a
        # client-facing condition.
        return error_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            error="bad_request",
            message=exc.message,
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
    - Email maps to a non-ACTIVE user (DISABLED / SUSPENDED /
      PENDING) → 401 indistinguishable from incorrect password,
      same as :func:`login_for_access_token`.

    Why peek for the new-user case: the user might take 30 seconds
    typing their name. If we consumed here, an interrupt between
    this call and ``POST /users`` would leave them with a verified
    intent but no way to actually finish without a fresh code.
    """
    # Peek mode — checks code validity, increments attempts on
    # mismatch (so brute-force is still rate-limited), but doesn't
    # delete on success.
    try:
        await verify_register_code(
            redis, email=body.email, code=body.code, consume=False
        )
    except VerificationError as exc:
        return _map_verification_error_to_response(exc)

    existing = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if existing is None:
        # New user — frontend should now show the "what should we call
        # you?" step and POST /users with the same code.
        return EmailLoginNewUser()

    if existing.status is UserStatus.PENDING:
        return error_response(
            status_code=status.HTTP_403_FORBIDDEN,
            error="account_pending",
            message="账号正在等待管理员审批",
        )
    if existing.status is not UserStatus.ACTIVE:
        # ``DISABLED`` / ``SUSPENDED`` collapse to the same opaque 401
        # as wrong code so admin lockouts don't leak.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is not active",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Existing ACTIVE user: now consume the code (commit to logging
    # in) and issue the JWT. ``consume`` raises ``not_found`` if the
    # code was somehow deleted between the peek and now; treat that
    # as ``invalid_code`` for the client.
    try:
        await verify_register_code(
            redis, email=body.email, code=body.code, consume=True
        )
    except VerificationError as exc:
        return _map_verification_error_to_response(exc)

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
    """
    try:
        user = await register_user(
            db,
            redis,
            email=body.email,
            code=body.code,
            name=body.name,
        )
    except RegisterError as exc:
        if exc.reason in ("username_taken", "email_taken"):
            return error_response(
                status_code=status.HTTP_409_CONFLICT,
                error=exc.reason,
                message=exc.message,
            )
        if exc.reason == "verification_too_many_attempts":
            return error_response(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                error=exc.reason,
                message=exc.message,
            )
        # ``verification_mismatch`` / ``verification_not_found`` —
        # collapsed to one client-facing reason (``invalid_code``)
        # because the user-facing message and recovery action are the
        # same.
        return error_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            error="invalid_code",
            message=exc.message,
        )

    # No ``last_login_at`` update or token issuance — the user is
    # ``PENDING`` and can't sign in yet. ``register_user`` already
    # committed the row.
    return RegisterResponse()


def _map_verification_error_to_response(exc: VerificationError):
    """Shared mapping used by the peek and consume paths in
    :func:`access_token_by_code`. Mirrors the registration endpoint's
    reason → status table so the frontend's error-display layer can
    branch uniformly."""
    if exc.reason == "too_many_attempts":
        return error_response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            error="verification_too_many_attempts",
            message=exc.message,
        )
    return error_response(
        status_code=status.HTTP_400_BAD_REQUEST,
        error="invalid_code",
        message=exc.message,
    )
