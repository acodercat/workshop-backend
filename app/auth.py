import asyncio
import logging
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from jwt.exceptions import InvalidTokenError
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.api.dependencies import get_db
from app.database import AsyncSessionLocal
from app.errors import AccountPendingError
from config import settings

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/access-tokens")


def _hash_password_sync(password: str) -> str:
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def _verify_password_sync(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except ValueError:
        # Raised by bcrypt when the stored hash is malformed.
        logger.warning("Malformed bcrypt hash encountered during verification")
        return False


async def get_password_hash(password: str) -> str:
    """Generate a bcrypt hash off the event loop (~250ms at rounds=12)."""
    return await asyncio.to_thread(_hash_password_sync, password)


async def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password off the event loop. Returns True iff the hash matches."""
    return await asyncio.to_thread(_verify_password_sync, plain_password, hashed_password)


async def get_user_by_username(db: AsyncSession, username: str) -> models.User | None:
    """Look up a user by their username — the login identifier."""
    stmt = select(models.User).where(models.User.username == username)
    result = await db.execute(stmt)
    return result.scalar()


async def authenticate_user(
    db: AsyncSession, username: str, password: str
) -> models.User | None:
    """Return the user iff the credentials are valid, regardless of
    status.

    Status checking is left to the calling endpoint so it can
    distinguish ``PENDING`` (surface ``account_pending`` so the user
    sees "awaiting admin approval") from ``DISABLED`` / ``SUSPENDED``
    (collapse to a generic 401 indistinguishable from wrong creds —
    admin-initiated lockouts shouldn't leak account state). Existing
    JWTs aren't revoked by a status change; bump ``token_version``
    separately if you need immediate force-logout.
    """
    user = await get_user_by_username(db, username)
    if user is None or not await verify_password(password, user.password_hash):
        return None
    return user


def assert_user_active(user: models.User, *, inactive_detail: str) -> None:
    """Gate sign-in on account status; no-op for an ``ACTIVE`` user.

    ``PENDING`` raises ``AccountPendingError`` (403) so the frontend can
    show "awaiting admin approval". ``DISABLED`` / ``SUSPENDED`` collapse
    to an opaque 401 carrying ``inactive_detail`` — kept generic (and,
    for password login, identical to the wrong-credentials message) so
    admin-initiated lockouts don't leak account state. The caller passes
    the detail because what's "opaque" differs by flow.
    """
    if user.status is models.UserStatus.PENDING:
        raise AccountPendingError("Account awaiting administrator approval")
    if user.status is not models.UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=inactive_detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Encode a JWT access token. Defaults to the configured expiration."""
    to_encode = data.copy()
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    to_encode["exp"] = datetime.now(timezone.utc) + expires_delta
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


#
# Share-view tokens are a separate JWT class scoped to a single share-link
# recipient. Three properties make them safe to coexist with user tokens:
#
#   1. They carry ``scope == "share-view"``; the user-token decoder rejects
#      anything with a ``scope`` field, and the share decoder rejects
#      anything without one. Two tokens, two decoders, zero crossover.
#   2. ``sub`` is the share token (URL-safe) rather than a user phone, so
#      a forged token has nothing to bind to a user account even if the
#      decoder were tricked.
#   3. TTL is short (15 minutes by default) — the recipient is expected to
#      be in an active session of viewing; longer would just expand the
#      window for a stolen token.

SHARE_VIEW_SCOPE = "share-view"


def create_share_view_token(share_token: str) -> tuple[str, datetime]:
    """Mint a short-lived JWT bound to a single share token.

    Returns the encoded token and its absolute expiry so the caller
    (the unlock endpoint) can hand both to the client without
    decoding the JWT a second time to read ``exp``.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.share_view_token_expire_minutes
    )
    payload = {
        "sub": share_token,
        "scope": SHARE_VIEW_SCOPE,
        "exp": expires_at,
    }
    return (
        jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm),
        expires_at,
    )


def decode_share_view_token(token: str) -> str | None:
    """Verify a share-view JWT and return the bound share token.

    Returns None on any failure (bad signature, expired, wrong scope,
    missing sub) — caller decides whether None means 401-locked or
    just "no token presented."
    """
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.algorithm]
        )
    except InvalidTokenError as exc:
        logger.info("Rejected share-view token: %s: %s", type(exc).__name__, exc)
        return None
    if payload.get("scope") != SHARE_VIEW_SCOPE:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


async def _decode_and_get_user(db: AsyncSession, token: str) -> models.User:
    """Decode the JWT, verify its token_version, and return the matching user.

    Tokens carrying ``scope`` are rejected — those are share-view tokens
    that have no user identity bound to them and must never be accepted
    on a user-auth path.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.algorithm]
        )
    except InvalidTokenError as exc:
        logger.info("Rejected token: %s: %s", type(exc).__name__, exc)
        raise credentials_exception

    if "scope" in payload:
        # A scoped token (share-view) reaching the user-auth path is a
        # caller mistake at best, an attempted privilege escalation at
        # worst. Refuse without distinguishing the two.
        raise credentials_exception

    # ``sub`` was previously the user's phone number; we backfilled
    # pre-existing values into ``username`` 1:1, so old tokens keep
    # working unchanged after the rename.
    username: str | None = payload.get("sub")
    token_version: int = payload.get("token_version", 0)
    if username is None:
        raise credentials_exception

    user = await get_user_by_username(db, username=username)
    if user is None or user.token_version != token_version:
        raise credentials_exception
    return user


async def authenticate_and_get_user(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> models.User:
    """FastAPI dependency: extract Bearer token from header and validate."""
    return await _decode_and_get_user(db, token)


async def authenticate_for_streaming(
    token: str = Depends(oauth2_scheme),
) -> models.User:
    """Auth for SSE / WebSocket / other long-lived response endpoints.

    Opens its own short-lived ``AsyncSession`` for the JWT lookup and
    closes it before returning, so FastAPI's request-scoped
    ``AsyncExitStack`` never holds a DB connection across the response
    lifetime. The standard :func:`authenticate_and_get_user` injects
    :func:`get_db`, whose yield-style session would stay checked out for
    the full SSE duration — long enough to trip Postgres'
    ``idle_in_transaction_session_timeout`` and lose the connection.

    Mirrors sse-starlette's recommended pattern of creating database
    sessions inside the streaming context rather than as request-scoped
    dependencies.
    """
    async with AsyncSessionLocal() as db:
        return await _decode_and_get_user(db, token)


def revoke_user_tokens(user: models.User) -> None:
    """
    Invalidate every previously issued token for this user by bumping
    `token_version`. Caller is responsible for committing the session.

    Use after password change, "logout everywhere", or admin-forced logout.
    """
    user.token_version += 1
