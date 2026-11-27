"""The staff JWT class, and its exclusion from the other two.

``app/auth.py`` issues three token classes distinguished only by a
``scope`` claim. The security property is that no token of one class
authenticates as another, in **either** direction — a customer token must
not reach the console, and a staff token must not reach a customer
endpoint. Both directions are asserted here, because each is enforced by
a different decoder and a change to either one alone would open a hole
the other's tests would not see.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException

from app import auth
from app.errors import StaffAuthenticationError
from app.models.staff import Staff, StaffStatus
from config import settings


def _staff(**overrides) -> Staff:
    """A staff row as the DB would hand it back, without a DB."""
    defaults = dict(
        id=7,
        username="alice",
        password_hash="x",
        name="Alice",
        status=StaffStatus.ACTIVE,
        token_version=0,
    )
    return Staff(**{**defaults, **overrides})


def _encode(payload: dict) -> str:
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


# --- the happy path ----------------------------------------------------


def test_round_trips_username_and_token_version() -> None:
    token, expires_at = auth.create_staff_access_token(_staff(token_version=3))

    claims = auth.decode_staff_token(token)

    assert claims is not None
    assert claims.username == "alice"
    assert claims.token_version == 3
    assert expires_at > datetime.now(UTC)


def test_expiry_honours_the_admin_specific_ttl() -> None:
    # The console's TTL is deliberately shorter than the customer token's
    # week; a regression that reused the customer setting would widen the
    # window a stolen console token stays live.
    _, expires_at = auth.create_staff_access_token(_staff())

    expected = datetime.now(UTC) + timedelta(minutes=settings.admin_access_token_expire_minutes)
    assert abs((expires_at - expected).total_seconds()) < 5


# --- class exclusion, both directions ----------------------------------


def test_customer_token_is_not_a_staff_token() -> None:
    # A customer access token carries no ``scope`` at all.
    customer = auth.create_access_token({"sub": "axon_1"})

    assert auth.decode_staff_token(customer) is None


def test_share_view_token_is_not_a_staff_token() -> None:
    share, _ = auth.create_share_view_token("some-share-token")

    assert auth.decode_staff_token(share) is None


def test_staff_token_is_not_a_share_view_token() -> None:
    staff_token, _ = auth.create_staff_access_token(_staff())

    assert auth.decode_share_view_token(staff_token) is None


async def test_staff_token_is_rejected_where_a_customer_token_is_expected() -> None:
    # The customer decoder refuses anything carrying a scope — before it
    # touches the database or the identity provider, which is why passing
    # ``None`` for both proves the property:
    # a staff token must be dead on arrival at a customer endpoint, not
    # merely fail to match a user row.
    staff_token, _ = auth.create_staff_access_token(_staff())

    with pytest.raises(HTTPException) as exc_info:
        await auth._authenticate(None, staff_token, None)

    assert exc_info.value.status_code == 401


# --- failure modes all collapse to None --------------------------------


def test_rejects_a_foreign_signature() -> None:
    forged = jwt.encode(
        {"sub": "alice", "scope": auth.STAFF_SCOPE, "token_version": 0},
        # Long enough that PyJWT doesn't warn about the key rather than
        # the thing under test.
        "an-attackers-secret-that-is-not-ours-0123456789",
        algorithm=settings.algorithm,
    )

    assert auth.decode_staff_token(forged) is None


def test_rejects_an_expired_token() -> None:
    stale = _encode(
        {
            "sub": "alice",
            "scope": auth.STAFF_SCOPE,
            "token_version": 0,
            "exp": datetime.now(UTC) - timedelta(seconds=1),
        }
    )

    assert auth.decode_staff_token(stale) is None


def test_rejects_an_unknown_scope() -> None:
    wrong_scope = _encode({"sub": "alice", "scope": "something-else", "token_version": 0})

    assert auth.decode_staff_token(wrong_scope) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"scope": "staff", "token_version": 0},  # no subject
        {"sub": 7, "scope": "staff", "token_version": 0},  # subject not a string
        {"sub": "alice", "scope": "staff", "token_version": "0"},  # version a string
    ],
)
def test_rejects_ill_typed_claims(payload: dict) -> None:
    # A well-signed token with the wrong claim types would otherwise be
    # compared against ``staff.token_version`` as a string and never match
    # — a silent, confusing 401 instead of an explicit refusal.
    assert auth.decode_staff_token(_encode(payload)) is None


# --- the dependency's opaque failure -----------------------------------


async def test_missing_credentials_raise_the_same_error_as_a_bad_token() -> None:
    # No Authorization header at all: HTTPBearer(auto_error=False) yields
    # None, and the dependency must produce the same opaque error rather
    # than a distinguishable "Not authenticated".
    with pytest.raises(StaffAuthenticationError):
        await auth.authenticate_and_get_staff(request=None, db=None, credentials=None)
