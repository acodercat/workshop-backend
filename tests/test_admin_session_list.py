"""The platform-wide session list names the owner, and opens nothing.

``GET /sessions`` is the console's cross-user view of the same rows
``GET /users/{id}/sessions`` shows one user at a time (pinned in
:mod:`tests.test_admin_session_usage`). It differs by the owner column,
so these pin that the owner reaches the row — including when there is
none — and that the module still exposes exactly one route.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import uuid
from datetime import UTC, datetime
from typing import NamedTuple

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import session as admin_session, session_rows
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.session import AgentType, Session, SessionStatus, Tier
from app.models.staff import Staff, StaffStatus

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_OWNED = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _session(session_id: uuid.UUID, user_id: int | None) -> Session:
    session = Session(
        id=session_id,
        user_id=user_id,
        title="调研报告",
        model_id="qwen-main",
        tier=Tier.STANDARD,
        thinking_enabled=True,
        agent_type=AgentType.FULL,
        status=SessionStatus.COMPLETE,
    )
    session.created_at = session.updated_at = _NOW
    session.deleted_at = None
    return session


class _TurnRow(NamedTuple):
    """Shaped like the ``Row`` the grouped query returns: the service
    reads it by label, so a bare tuple would not exercise the same
    code."""

    session_id: uuid.UUID
    turn_count: int
    total_tokens: int


class _Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _FakeDb:
    """Serves the endpoint's fixed call order: the count, the joined
    page of ``(session, owner_username, owner_name)``, then the grouped
    turn aggregate."""

    def __init__(self, page: list, turn_rows: list) -> None:
        self._total = len(page)
        self._results = [_Rows(page), _Rows(turn_rows)]

    async def scalar(self, _stmt):
        return self._total

    async def execute(self, _stmt):
        return self._results.pop(0)


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


def _items(db: _FakeDb, monkeypatch, spend: dict) -> list:
    async def fake_spend(_db, _session_ids):
        return spend

    monkeypatch.setattr(session_rows.analytics, "session_spend", fake_spend)
    return _client(db).get("/sessions").json()["items"]


def test_owner_and_usage_land_on_the_row(monkeypatch) -> None:
    """The username comes off the join, turns and tokens off the grouped
    ``session_turn`` query, credits off the ledger's own module —
    3_000_000_000 µ-credits is 300 whole credits at the console's 1e7
    rate."""
    db = _FakeDb(
        [(_session(_OWNED, 42), "axon_42", "王小明")],
        [_TurnRow(_OWNED, 3, 120_000)],
    )

    (row,) = _items(db, monkeypatch, {_OWNED: 3_000_000_000})

    # Both name columns, off the one join: the console shows the username
    # with the display name under it, the same identity the leaderboards
    # render beside this list on the dashboard.
    assert (row["user_id"], row["owner_username"], row["owner_name"]) == (
        42,
        "axon_42",
        "王小明",
    )
    assert (row["turn_count"], row["total_tokens"], row["spent_credits"]) == (3, 120_000, 300)
    # ``model_id`` is what ran and was billed; ``tier`` is only what the
    # customer picked, and the console does not carry it.
    assert row["model_id"] == "qwen-main"
    assert "tier" not in row


def test_the_row_carries_no_conversation(monkeypatch) -> None:
    """The list is the only session surface on this side, so it must not
    grow a content field by way of a schema someone reuses."""
    db = _FakeDb([(_session(_OWNED, 42), "axon_42", "王小明")], [])

    (row,) = _items(db, monkeypatch, {})

    assert "messages" not in row and "blocks" not in row


def test_the_module_exposes_one_route_and_no_session_detail() -> None:
    """A conversation is not readable here. A second route arriving by
    habit — ``/sessions/{session_id}`` above all — is the regression this
    guards."""
    assert [(route.path, sorted(route.methods)) for route in admin_session.router.routes] == [
        ("/sessions", ["GET"])
    ]
