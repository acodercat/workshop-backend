import asyncio
import functools
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.api.dependencies import get_db
from app.common.password import BCRYPT_MAX_BYTES
from app.database import AsyncSessionLocal
from app.errors import StaffAuthenticationError
from app.rate_limit import set_rate_limit_principal
from config import settings

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/access-tokens")


def _hash_password_sync(password: str) -> str:
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def _verify_password_sync(plain_password: str, hashed_password: str) -> bool:
    if len(plain_password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        # bcrypt 5 raises on such input, and no stored hash can match it:
        # every hash was made from at most that many bytes.
        return False
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


@functools.cache
def _absent_account_hash() -> str:
    return _hash_password_sync(secrets.token_urlsafe(16))


async def _password_matches(password: str, password_hash: str | None) -> bool:
    """:func:`verify_password`, also when no account matched (``None``).

    That case still runs a bcrypt round, against a throwaway hash at the
    configured cost, and answers ``False`` — so an unknown username takes
    as long as a wrong password and response time cannot tell them apart.
    """
    if password_hash is None:
        await verify_password(password, await asyncio.to_thread(_absent_account_hash))
        return False
    return await verify_password(password, password_hash)


async def get_user_by_username(db: AsyncSession, username: str) -> models.User | None:
    """Look up a user by their username — the login identifier."""
    stmt = select(models.User).where(models.User.username == username)
    result = await db.execute(stmt)
    return result.scalar()


async def authenticate_user(db: AsyncSession, username: str, password: str) -> models.User | None:
    """Return the user iff the credentials are valid, regardless of
    status.

    Status checking is left to the caller via :func:`is_user_active` —
    the password flow collapses ``DISABLED`` / ``SUSPENDED`` into the same
    401 as wrong credentials so admin-initiated lockouts don't leak account
    state. A status flip
    also blocks any *existing* JWT at its next request (see the status
    gate in :func:`_decode_and_get_user`); ``token_version`` is still the
    lever for force-logout on a password / email change.
    """
    user = await get_user_by_username(db, username)
    if not await _password_matches(password, user.password_hash if user else None):
        return None
    return user


def is_user_active(user: models.User) -> bool:
    """Whether the account may sign in and hold a session right now.

    Checked at sign-in *and* on every authenticated request, so a flip to
    ``DISABLED`` / ``SUSPENDED`` takes effect on the next call without a
    ``token_version`` bump. What a refusal looks like is the caller's: the
    password flow keeps it indistinguishable from wrong credentials, the code
    flow can say it plainly. Mirrors :func:`is_staff_active`.
    """
    return user.status is models.UserStatus.ACTIVE


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Encode a JWT access token. Defaults to the configured expiration."""
    to_encode = data.copy()
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    to_encode["exp"] = datetime.now(UTC) + expires_delta
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


#
# Share-view tokens are a separate JWT class scoped to a single share-link
# recipient. Three properties make them safe to coexist with user tokens:
#
#   1. They carry ``scope == "share-view"``; the user-token decoder rejects
#      anything with a ``scope`` field, and the share decoder rejects
#      anything without one. Two tokens, two decoders, zero crossover.
#   2. ``sub`` is the share token (URL-safe) rather than a username, so
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
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.share_view_token_expire_minutes)
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
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
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
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
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
    if not is_user_active(user):
        # Same opaque 401 as a bad token so a lockout never leaks account
        # state; the row is already loaded, so this is a free check.
        raise credentials_exception
    return user


async def authenticate_and_get_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> models.User:
    """FastAPI dependency: extract Bearer token from header and validate.

    Stamps the resolved identity as the rate-limit principal so the
    limiter keys authenticated requests on the user rather than the
    client IP (NAT/CGNAT-shared IPs otherwise share one bucket). See
    :func:`app.rate_limit.set_rate_limit_principal`.
    """
    user = await _decode_and_get_user(db, token)
    set_rate_limit_principal(request, "user", user.id)
    return user


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


#
# Staff tokens are the third and last JWT class, issued to admin-console
# operators. All three live in this module on purpose: they are defined by
# their mutual exclusion, and that property is only auditable if the
# decoders sit next to each other.
#
#   ==============  ==================  ==============================
#   Token           ``scope`` claim     Decoder
#   ==============  ==================  ==============================
#   user access     *(absent)*          :func:`_decode_and_get_user`
#   share view      ``"share-view"``    :func:`decode_share_view_token`
#   staff access    ``"staff"``         :func:`decode_staff_token`
#   ==============  ==================  ==============================
#
# ``_decode_and_get_user`` rejects any token carrying a ``scope``, so a
# staff token can never authenticate a customer request.
# :func:`decode_staff_token` requires ``scope == "staff"``, so neither a
# customer token (no scope) nor a share-view token reaches the console.
# Each decoder fails closed.
#
# ``sub`` is the staff username, resolved against the ``staff`` table
# only — a forged token has nothing to bind to a customer account.
#
# TTL is a workday rather than the customer token's week: the console is
# the highest-privilege surface and is used from a workstation, so a
# stolen token's window should close by the next morning.

STAFF_SCOPE = "staff"

# ``HTTPBearer``, not ``OAuth2PasswordBearer``: the console signs in with
# a JSON body, and advertising an OAuth2 *password-form* flow in the
# schema would describe an endpoint that does not exist — Swagger's
# Authorize dialog would post form-encoded fields and fail. Plain bearer
# is what the console actually sends.
#
# ``auto_error=False`` so a missing or malformed ``Authorization`` header
# reaches our own handling below and collapses into the same opaque 401
# as every other failure, instead of HTTPBearer's distinguishable
# "Not authenticated".
staff_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class StaffTokenClaims:
    """The claims the console cares about once signatures check out.

    Frozen because a decoded token describes a past event; nothing
    downstream should edit it in place.
    """

    username: str
    token_version: int


def create_staff_access_token(staff: models.Staff) -> tuple[str, datetime]:
    """Mint a staff access token.

    Returns the encoded JWT **and** its absolute expiry so the login
    endpoint doesn't have to decode what it just encoded (same shape as
    :func:`create_share_view_token`).
    """
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.admin_access_token_expire_minutes)
    payload = {
        "sub": staff.username,
        "scope": STAFF_SCOPE,
        "token_version": staff.token_version,
        "exp": expires_at,
    }
    encoded = jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
    return encoded, expires_at


def decode_staff_token(token: str) -> StaffTokenClaims | None:
    """Verify a staff JWT and return its claims.

    Returns ``None`` on every failure mode — bad signature, expired,
    wrong or missing scope, ill-typed claims — so the caller raises one
    indistinguishable 401 instead of telling an attacker which check
    failed.
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except InvalidTokenError as exc:
        logger.info("Rejected staff token: %s: %s", type(exc).__name__, exc)
        return None

    if payload.get("scope") != STAFF_SCOPE:
        # A customer or share-view token reaching the console is a client
        # bug at best and an escalation attempt at worst. Refuse without
        # distinguishing the two.
        return None

    username = payload.get("sub")
    token_version = payload.get("token_version", 0)
    if not isinstance(username, str) or not isinstance(token_version, int):
        return None
    return StaffTokenClaims(username=username, token_version=token_version)


async def get_staff_by_username(db: AsyncSession, username: str) -> models.Staff | None:
    """Look up a staff account by its login identifier."""
    stmt = select(models.Staff).where(models.Staff.username == username)
    result = await db.execute(stmt)
    return result.scalar()


async def authenticate_staff(db: AsyncSession, username: str, password: str) -> models.Staff | None:
    """Return the staff row iff the credentials are valid, regardless of
    status.

    Status is left to the caller so a ``DISABLED`` account yields the
    *same* opaque 401 as a wrong password — a former operator should not
    be able to confirm their account still exists. Mirrors
    :func:`authenticate_user`.
    """
    staff = await get_staff_by_username(db, username)
    if not await _password_matches(password, staff.password_hash if staff else None):
        return None
    return staff


def is_staff_active(staff: models.Staff) -> bool:
    """Whether the account may hold a session right now.

    Checked at login *and* on every authenticated request, so flipping a
    departing operator to ``DISABLED`` takes effect on their next call
    without a ``token_version`` bump.
    """
    return staff.status is models.StaffStatus.ACTIVE


async def authenticate_and_get_staff(
    request: Request,
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(staff_bearer_scheme),
) -> models.Staff:
    """FastAPI dependency: resolve and validate the calling operator.

    Every failure — absent header, malformed token, wrong scope, unknown
    username, stale ``token_version``, deactivated account — collapses to
    the same :class:`~app.errors.StaffAuthenticationError`. Telling the
    caller *why* their token was refused is free reconnaissance against
    the highest-privilege surface in the system.

    Stamps the operator as the rate-limit principal so slowapi keys
    console traffic on them rather than on the shared office egress IP.

    Endpoints consume this through ``StaffContext``
    (:mod:`app.api.admin.dependencies`), which packages the operator with
    the request metadata the audit trail records.
    """
    unauthenticated = StaffAuthenticationError("Could not validate credentials")

    if credentials is None:
        raise unauthenticated

    claims = decode_staff_token(credentials.credentials)
    if claims is None:
        raise unauthenticated

    staff = await get_staff_by_username(db, claims.username)
    if staff is None or staff.token_version != claims.token_version:
        raise unauthenticated
    if not is_staff_active(staff):
        # Already-loaded row, so this costs nothing and makes a DISABLED
        # flip effective on the very next request.
        raise unauthenticated

    set_rate_limit_principal(request, "staff", staff.id)
    return staff


def revoke_staff_tokens(staff: models.Staff) -> None:
    """Invalidate every token issued to this account so far.

    Use after a password change, an explicit "sign out everywhere", or a
    suspected credential leak. Caller commits.
    """
    staff.token_version += 1
