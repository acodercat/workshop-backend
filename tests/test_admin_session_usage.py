"""The console's session list reports usage, and cannot open a session.

Both halves are the same decision. ``GET /sessions/{id}`` was removed
rather than audited — a conversation is the most private thing the
platform holds and no console question needs one — so the row itself has
to answer "was this session expensive, and why". These pin that the three
usage figures reach the response, and that the content endpoint stays
gone.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import uuid
from datetime import UTC, datetime
from typing import NamedTuple

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import user as admin_user
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.session import AgentType, Session, SessionStatus, Tier
from app.models.staff import Staff, StaffStatus
from app.models.user import User, UserStatus

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_BUSY = uuid.UUID("11111111-1111-1111-1111-111111111111")
_FRESH = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _session(session_id: uuid.UUID) -> Session:
    session = Session(
        id=session_id,
        user_id=42,
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
    """Shaped like the ``Row`` the grouped query returns: the endpoint
    reads it by label, so a bare tuple would not exercise the same code."""

    session_id: uuid.UUID
    turn_count: int
    total_tokens: int


class _Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _FakeDb:
    """Serves the endpoint's fixed call order: the user PK lookup, the
    count, the page of sessions, then the grouped turn aggregate."""

    def __init__(self, turn_rows: list) -> None:
        self._turn_rows = turn_rows

    async def get(self, _model, _pk):
        return User(
            id=42,
            username="axon_42",
            email="a@example.com",
            password_hash="x",
            status=UserStatus.ACTIVE,
            credit_balance=0,
            token_version=0,
        )

    async def scalar(self, _stmt):
        return 2

    async def scalars(self, _stmt):
        return _Rows([_session(_BUSY), _session(_FRESH)])

    async def execute(self, _stmt):
        return _Rows(self._turn_rows)


def _client(db: _FakeDb) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_user.router)
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


def test_usage_lands_on_each_row(monkeypatch) -> None:
    """Turns and tokens come from the grouped ``session_turn`` query,
    credits from the ledger's own module — 3_000_000_000 µ-credits is
    300 whole credits at the console's 1e7 rate."""
    db = _FakeDb([_TurnRow(_BUSY, 3, 120_000)])

    async def fake_spend(_db, session_ids):
        assert list(session_ids) == [_BUSY, _FRESH]
        return {_BUSY: 3_000_000_000}

    monkeypatch.setattr(admin_user.analytics, "session_spend", fake_spend)

    body = _client(db).get("/users/42/sessions").json()

    busy, fresh = body["items"]
    assert (busy["turn_count"], busy["total_tokens"], busy["spent_credits"]) == (3, 120_000, 300)
    # A session with no turns and nothing settled is absent from both
    # aggregates; the row still renders, as zeros rather than nulls.
    assert (fresh["turn_count"], fresh["total_tokens"], fresh["spent_credits"]) == (0, 0, 0)


def test_the_row_names_the_model_that_ran_not_the_tier_picked(monkeypatch) -> None:
    """``tier`` is the capability the customer chose; ``model_id`` is
    what it resolved to and what the cost was billed against. The
    console explains cost, so it carries the second and not the first."""

    async def fake_spend(_db, _session_ids):
        return {}

    monkeypatch.setattr(admin_user.analytics, "session_spend", fake_spend)

    (row, _) = _client(_FakeDb([])).get("/users/42/sessions").json()["items"]

    assert row["model_id"] == "qwen-main"
    assert "tier" not in row


def test_the_row_carries_no_conversation(monkeypatch) -> None:
    """The list is the only session surface, so it must not grow a
    content field by way of a schema someone reuses."""

    async def fake_spend(_db, _session_ids):
        return {}

    monkeypatch.setattr(admin_user.analytics, "session_spend", fake_spend)

    (row, _) = _client(_FakeDb([])).get("/users/42/sessions").json()["items"]

    assert "messages" not in row and "blocks" not in row
