"""Password sign-in never says whether an account exists.

Not by its answer — an unknown name, a wrong password and a disabled
account refuse with the same error — and not by its timing: returning
before bcrypt made a miss ~250 ms faster, so response time enumerated
accounts.
"""

from unittest.mock import AsyncMock

import pytest

from app import auth
from config import settings


@pytest.fixture(autouse=True)
def _cheap_bcrypt(monkeypatch):
    monkeypatch.setattr(settings, "bcrypt_rounds", 4)
    auth._absent_account_hash.cache_clear()
    yield
    auth._absent_account_hash.cache_clear()


@pytest.mark.parametrize(
    ("lookup", "authenticate"),
    [
        ("get_user_by_username", auth.authenticate_user),
        ("get_staff_by_username", auth.authenticate_staff),
    ],
)
async def test_an_unknown_name_still_runs_bcrypt(monkeypatch, lookup, authenticate) -> None:
    monkeypatch.setattr(auth, lookup, AsyncMock(return_value=None))
    checked: list[str] = []
    real = auth._verify_password_sync

    def spy(plain, hashed):
        checked.append(hashed)
        return real(plain, hashed)

    monkeypatch.setattr(auth, "_verify_password_sync", spy)

    assert await authenticate(AsyncMock(), "nobody", "guess") is None
    assert len(checked) == 1
    assert checked[0].startswith("$2b$04$")  # a real round at the configured cost


@pytest.mark.parametrize("found", [None, "disabled"])
async def test_a_disabled_account_is_refused_like_a_wrong_password(monkeypatch, found) -> None:
    # Same error type and message either way, so a lockout never confirms
    # that the account exists.
    from unittest.mock import MagicMock

    from app.api.public import auth as endpoint
    from app.errors import InvalidCredentialsError
    from app.models.user import UserStatus
    from app.rate_limit import limiter

    user = None if found is None else MagicMock(status=UserStatus.DISABLED)
    monkeypatch.setattr(endpoint, "authenticate_user", AsyncMock(return_value=user))
    monkeypatch.setattr(limiter, "enabled", False)

    with pytest.raises(InvalidCredentialsError) as caught:
        await endpoint.login_for_access_token(
            request=MagicMock(),
            db=AsyncMock(),
            form_data=MagicMock(username="u", password="p"),
        )

    assert caught.value.message == "Incorrect username or password"
