"""The refusal a customer request gets when its token cannot identify a user.

Pinned whole — status, body and challenge header — because clients sign the
user out on exactly this response, and every way a token can fail must be
indistinguishable from the others.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.api.dependencies import get_db
from app.exception_handlers import install_error_handlers
from app.models.user import User, UserStatus


def _client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.dependency_overrides[get_db] = lambda: MagicMock()

    @app.get("/whoami")
    async def whoami(user: User = Depends(auth.authenticate_and_get_user)) -> dict[str, str]:
        return {"username": user.username}

    return TestClient(app)


def _assert_refused(response) -> None:
    assert response.status_code == 401
    assert response.json() == {
        "error": {"type": "unauthorized", "message": "Could not validate credentials"}
    }
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_token_that_does_not_verify_is_refused() -> None:
    _assert_refused(_client().get("/whoami", headers={"Authorization": "Bearer not-a-jwt"}))


def test_a_share_view_token_is_refused_on_the_user_path() -> None:
    token, _ = auth.create_share_view_token("share-token")

    _assert_refused(_client().get("/whoami", headers={"Authorization": f"Bearer {token}"}))


@pytest.mark.parametrize(
    "user",
    [
        None,
        User(username="mina", token_version=5, status=UserStatus.ACTIVE),
        User(username="mina", token_version=0, status=UserStatus.DISABLED),
    ],
    ids=["no such user", "a revoked token", "an account that is not active"],
)
def test_a_valid_token_no_active_user_answers_is_refused(monkeypatch, user) -> None:
    monkeypatch.setattr(auth, "get_user_by_username", AsyncMock(return_value=user))
    token = auth.create_access_token({"sub": "mina", "token_version": 0})

    _assert_refused(_client().get("/whoami", headers={"Authorization": f"Bearer {token}"}))
