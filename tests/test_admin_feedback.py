"""``PATCH /feedback/{id}`` — the review queue's one mutation.

Same harness as the other admin endpoint tests: real app, overridden
``get_db`` / ``authenticate_and_get_staff``, a fake session serving the
PK lookup and the username scalar.
"""

from datetime import UTC, datetime
from typing import NamedTuple

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import feedback as admin_feedback
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.admin_audit_log import AdminAuditLog
from app.models.staff import Staff, StaffStatus
from app.models.user_feedback import FeedbackStatus, UserFeedback

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _OwnerRow(NamedTuple):
    """Shaped like the ``Row`` ``_owner_identity`` reads by label — the
    two name columns the console renders as one identity."""

    username: str
    name: str


class _OwnerResult:
    def __init__(self, row: _OwnerRow) -> None:
        self._row = row

    def one_or_none(self) -> _OwnerRow:
        return self._row


class _FakeDb:
    """``get`` serves the PK lookup, ``execute`` the owner identity."""

    def __init__(self, feedback: UserFeedback | None) -> None:
        self.feedback = feedback
        self.added: list = []
        self.commits = 0

    async def get(self, _model, _pk):
        return self.feedback

    async def execute(self, _stmt):
        return _OwnerResult(_OwnerRow("axon_42", "王小明"))

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def _feedback(status: FeedbackStatus = FeedbackStatus.OPEN) -> UserFeedback:
    feedback = UserFeedback(
        id=9,
        user_id=42,
        message="the export button 404s",
        image_keys=[],
        status=status,
    )
    feedback.created_at = _NOW
    feedback.updated_at = _NOW
    return feedback


def _client(db: _FakeDb) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_feedback.router)
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


def _audit_rows(db: _FakeDb) -> list[AdminAuditLog]:
    return [obj for obj in db.added if isinstance(obj, AdminAuditLog)]


def test_triage_applies_and_audits() -> None:
    db = _FakeDb(_feedback())

    response = _client(db).patch("/feedback/9", json={"status": "RESOLVED"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "RESOLVED"
    assert body["owner_username"] == "axon_42"
    (row,) = _audit_rows(db)
    assert row.action == "feedback.update"
    assert (row.target_type, row.target_id) == ("feedback", "9")
    assert row.payload == {"status": {"from": "OPEN", "to": "RESOLVED"}}
    assert db.commits == 1


def test_a_noop_triage_writes_no_audit_row() -> None:
    db = _FakeDb(_feedback(FeedbackStatus.RESOLVED))

    response = _client(db).patch("/feedback/9", json={"status": "RESOLVED"})

    assert response.status_code == 200
    assert _audit_rows(db) == []
    assert db.commits == 0


def test_unknown_feedback_is_404() -> None:
    response = _client(_FakeDb(feedback=None)).patch("/feedback/999", json={"status": "IGNORED"})

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "feedback_not_found"
