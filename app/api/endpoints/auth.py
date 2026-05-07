from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_redis
from app.auth import authenticate_user, create_access_token
from app.rate_limit import limiter
from app.schemas.auth import (
    AccessTokenResponse,
    RegisterCodeRequest,
    RegisterRequest,
)
from app.schemas.user import UserResponse
from app.services.auth import (
    RegisterError,
    VerificationError,
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
    """Authenticate the user and issue a JWT access token."""
    user = await authenticate_user(db, form_data.username, form_data.password)
    if not user:
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


# ---- registration ---------------------------------------------------------
#
# Two-step ``verify-then-create`` flow modelled as REST resources:
#
#   POST /auth/verification-codes  { email, locale }
#       → creates a verification code, sends via Aliyun DM. 204 on
#         success (resource is the email message; nothing useful to
#         echo back, and confirming receipt would leak existence).
#   POST /users                    { email, code, username, name, password }
#       → creates a user with status=PENDING (admin must promote to
#         ACTIVE before login is allowed). 201 with the new user row.
#
# ``POST /auth/verification-codes`` mirrors ``POST /auth/access-tokens``
# for login — both create short-lived auth-scoped resources. ``POST
# /users`` lives at the top level because users aren't an auth-scoped
# resource: ``GET /me`` is the read-self counterpart, future admin
# list/lookup would join the same ``/users`` collection.
#
# Rate limiting works on two layers:
#   - Per-IP via slowapi (``rate_limit_register_code`` / ``_register``)
#   - Per-email via Redis cooldown (settings.register_code_resend_cooldown_seconds)
#
# Errors map to HTTP statuses by ``reason``:
#   cooldown / too_many_attempts        → 429
#   email_send_failed                   → 502 (upstream failure)
#   verification_not_found / mismatch   → 400
#   email_taken / username_taken        → 409
#
# Generic 401 on login still applies for non-ACTIVE users — neither
# endpoint here issues a JWT.


@router.post("/auth/verification-codes", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.rate_limit_register_code)
async def create_verification_code(
    request: Request,
    body: RegisterCodeRequest,
    redis: aioredis.Redis = Depends(get_redis),
):
    """Mint a verification code and send it to ``email``.

    Returns 204 on success regardless of whether the email is already
    taken — telling the caller "this address is registered" here would
    leak account existence to anyone who can hit the endpoint. The
    user finds out at ``POST /users`` where they have to prove
    possession of the email anyway.
    """
    try:
        await request_register_code(
            redis,
            email=body.email,
            locale=body.locale,
        )
    except VerificationError as exc:
        if exc.reason == "cooldown":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=exc.message,
            ) from exc
        if exc.reason == "email_send_failed":
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=exc.message,
            ) from exc
        # Defensive: any other reason from this entry point is a
        # programmer error (verify path returns those reasons), not a
        # client-facing condition.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
        ) from exc


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(settings.rate_limit_register)
async def create_user(
    request: Request,
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Self-registration: validate the code and create the user with
    ``status=PENDING``.

    Doesn't return a JWT. The user can't log in yet anyway (PENDING),
    so handing back a token would just confuse the client. After an
    admin promotes them to ACTIVE the user logs in via
    ``POST /auth/access-tokens`` like everyone else.
    """
    try:
        user = await register_user(
            db,
            redis,
            email=body.email,
            code=body.code,
            username=body.username,
            name=body.name,
            password=body.password,
        )
    except RegisterError as exc:
        if exc.reason in ("username_taken", "email_taken"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=exc.message,
            ) from exc
        if exc.reason == "verification_too_many_attempts":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=exc.message,
            ) from exc
        # mismatch / not_found / anything else verification raised
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
        ) from exc

    return user
