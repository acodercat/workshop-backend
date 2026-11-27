"""The refusal a customer request gets when its token cannot identify a user.

Pinned whole — status, body and challenge header — because clients sign the
user out on exactly this response, and every way a token can fail must be
indistinguishable from the others.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.api.dependencies import get_db, get_identity_provider
from app.exception_handlers import install_error_handlers
from app.models.user import User, UserStatus


def _client(row=None) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    result = MagicMock()
    result.one_or_none.return_value = row
    db = AsyncMock()
    db.execute.return_value = result
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_identity_provider] = lambda: MagicMock()

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


def _session_row(*, revoked: bool = False, expired: bool = False, status=UserStatus.ACTIVE):
    now = datetime.now(UTC)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revoked_at=now if revoked else None,
        expires_at=now - timedelta(seconds=1) if expired else now + timedelta(days=1),
        checked_at=now,
        verified_at=now,
    )
    return (session, SimpleNamespace(username="mina", status=status), "subject-1")


@pytest.mark.parametrize(
    "row",
    [
        None,
        _session_row(revoked=True),
        _session_row(expired=True),
        _session_row(status=UserStatus.DISABLED),
    ],
    ids=["no such session", "a revoked session", "an expired session", "an inactive account"],
)
def test_a_valid_token_no_live_session_answers_is_refused(row) -> None:
    token = auth.create_access_token({"sub": "mina", "sid": str(uuid.uuid4())})

    _assert_refused(_client(row).get("/whoami", headers={"Authorization": f"Bearer {token}"}))
