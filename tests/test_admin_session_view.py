"""``GET /sessions/{id}`` — the console's one audited read.

What these tests pin is the privacy property: viewing a customer's
conversation always lands a ``session.view`` row naming both the session
and its owner, and a session that never existed leaves no row at all.
"""

import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import session as admin_session
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.admin_audit_log import AdminAuditLog
from app.models.message import Message, MessageRole
from app.models.session import AgentType, Session, SessionStatus, Tier
from app.models.staff import Staff, StaffStatus

_SESSION_ID = uuid.UUID("00000000-0000-0000-0000-00000000abcd")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _Scalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _FakeDb:
    def __init__(self, session: Session | None, messages: list) -> None:
        self.session = session
        self.messages = messages
        self.added: list = []
        self.commits = 0

    async def get(self, _model, _pk):
        return self.session

    async def scalars(self, _stmt):
        return _Scalars(self.messages)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


def _session() -> Session:
    session = Session(
        id=_SESSION_ID,
        user_id=42,
        title="trip planning",
        status=SessionStatus.COMPLETE,
        tier=Tier.STANDARD,
        agent_type=AgentType.FULL,
        model_id="m",
    )
    session.created_at = _NOW
    session.updated_at = _NOW
    session.deleted_at = _NOW  # soft-deleted: still viewable by support
    return session


def _message() -> Message:
    message = Message(
        id=1,
        session_id=_SESSION_ID,
        role=MessageRole.USER,
        blocks=[{"type": "text", "text": "hi"}],
    )
    message.created_at = _NOW
    return message


def _client(db: _FakeDb) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_session.router)
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


def test_view_returns_content_and_audits_who_looked_at_whom() -> None:
    db = _FakeDb(_session(), [_message()])

    response = _client(db).get(f"/sessions/{_SESSION_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == 42
    assert body["deleted_at"] is not None  # soft-deleted, still served
    assert body["messages"][0]["blocks"] == [{"type": "text", "text": "hi"}]

    (row,) = [obj for obj in db.added if isinstance(obj, AdminAuditLog)]
    assert row.action == "session.view"
    assert (row.target_type, row.target_id) == ("session", str(_SESSION_ID))
    assert row.payload == {"user_id": 42}
    assert db.commits == 1


def test_unknown_session_is_404_and_leaves_no_trace() -> None:
    db = _FakeDb(session=None, messages=[])

    response = _client(db).get(f"/sessions/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "session_not_found"
    assert db.added == []
