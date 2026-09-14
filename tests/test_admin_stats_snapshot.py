"""The console home's snapshot row, over HTTP.

The two figures behind it that are new to the wire — the account total
on ``/stats/users`` and the conversation count on ``/stats/sessions`` —
and the one window value that makes "today" possible: ``days=1`` has to
reach the service as one day, not be clamped to the default like any
other unoffered integer. Nothing else in the suite exercises these
endpoints over HTTP.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import stats as admin_stats
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.common.business_time import window_start
from app.exception_handlers import install_error_handlers
from app.models.staff import Staff, StaffStatus


def _client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_stats.router)
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[authenticate_and_get_staff] = lambda: Staff(
        id=7,
        username="alice",
        password_hash="x",
        name="Alice",
        status=StaffStatus.ACTIVE,
        token_version=0,
    )
    return TestClient(app)


@pytest.fixture
def session_calls(monkeypatch) -> list[datetime]:
    calls: list[datetime] = []

    async def fake(db, *, since):
        calls.append(since)
        return 12, 5

    monkeypatch.setattr(admin_stats.session_service, "count_opened_since", fake)
    return calls


@pytest.fixture
def user_counts(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def registered(db, *, since):
        calls.append("registered")
        return 3

    async def total(db):
        calls.append("total")
        return 156

    monkeypatch.setattr(admin_stats.user_service, "count_registered_since", registered)
    monkeypatch.setattr(admin_stats.user_service, "count_all", total)
    return calls


def test_the_session_count_echoes_its_window(session_calls) -> None:
    body = _client().get("/stats/sessions?days=30").json()

    assert body == {"days": 30, "sessions": 12, "users": 5}
    assert len(session_calls) == 1


def test_one_day_is_today_not_the_default(session_calls) -> None:
    """The snapshot tiles ask ``days=1``. Before it was offered, the clamp
    turned that into 30 days without saying so — a "today" tile showing a
    month. The resolved window is echoed, so the body proves it as well
    as the service call does."""
    body = _client().get("/stats/sessions?days=1").json()

    assert body["days"] == 1
    # Aligned to the start of the business day, not now-minus-24h: the
    # bound the endpoint passed is the one ``window_start(1)`` computes.
    # Exact, not approximate — both are the same midnight unless the test
    # itself runs across one.
    assert session_calls[0] == window_start(1)


def test_an_unoffered_window_still_falls_back(session_calls) -> None:
    # Offering 1 must not have widened the set to "any integer".
    assert _client().get("/stats/sessions?days=2").json()["days"] == 30


def test_the_user_stats_carry_the_total_beside_the_flow(user_counts) -> None:
    body = _client().get("/stats/users?days=7").json()

    assert body == {"days": 7, "new_users": 3, "total_users": 156}
    assert user_counts == ["registered", "total"]
