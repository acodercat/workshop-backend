"""bcrypt's 72-byte bound, enforced in bytes at every new-password schema.

Operator and share-link passwords are hashed here; customer passwords
belong to the identity provider.

bcrypt 5 raises on longer input instead of truncating, so a password the
schema let through would 500 at hashing time.
"""

import bcrypt
import pytest
from pydantic import ValidationError

from app.auth import verify_password
from app.schemas.admin.staff import StaffPasswordReset


def test_operator_password_is_bounded_in_bytes() -> None:
    with pytest.raises(ValidationError):
        StaffPasswordReset(password="a" * 73)


async def test_an_over_long_password_simply_does_not_match() -> None:
    hashed = bcrypt.hashpw(b"secret", bcrypt.gensalt(rounds=4)).decode()

    assert await verify_password("a" * 73, hashed) is False
