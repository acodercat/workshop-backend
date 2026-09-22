"""``POST /invite-codes`` — the console's one invite mutation.

An invite is an access grant, so the properties worth pinning are what
the console is *not* allowed to issue and what the trail must record.
Same harness as the other admin endpoint tests: real app, overridden
``get_db`` / ``authenticate_and_get_staff``, and a fake session that
records what the endpoint added.
"""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import invite as admin_invite
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.invite_code import InviteCode
from app.models.staff import Staff, StaffStatus

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeSavepoint:
    """The service inserts inside a savepoint so a code collision can be
    retried without losing the surrounding transaction."""

    async def __aenter__(self) -> "_FakeSavepoint":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


class _FakeDb:
    """Serves the service's collision probe and records the insert."""

    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return None  # no code collides

    def begin_nested(self) -> _FakeSavepoint:
        return _FakeSavepoint()

    def add(self, obj) -> None:
        self.added.append(obj)
        if isinstance(obj, InviteCode):
            obj.id = 1
            obj.used_count = 0
            obj.enabled = True
            obj.created_at = _NOW

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def _client(db: _FakeDb) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_invite.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[authenticate_and_get_staff] = lambda: Staff(
        id=7,
        username="alice",
        password_hash="x",
        name="Alice",
        status=StaffStatus.ACTIVE,
        token_version=0,
    )
    return TestClient(app)


def test_issuing_mints_a_system_code_and_commits_it() -> None:
    db = _FakeDb()

    response = _client(db).post(
        "/invite-codes",
        json={"max_uses": 5, "note": "conference booth"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["origin"] == "SYSTEM"
    # A console-issued code is the platform's, never a user's — an
    # operator must not be able to mint someone else's referral code
    # and hand out the reward attached to it.
    assert body["owner_user_id"] is None
    assert body["used_count"] == 0
    assert db.commits == 1


@pytest.mark.parametrize(
    "body",
    [
        {"max_uses": 0},  # below the floor
        {"max_uses": 10_001},  # above the ceiling
    ],
)
def test_a_rejected_body_writes_nothing(body: dict) -> None:
    db = _FakeDb()

    response = _client(db).post("/invite-codes", json=body)

    assert response.status_code == 422
    assert db.added == [] and db.commits == 0
