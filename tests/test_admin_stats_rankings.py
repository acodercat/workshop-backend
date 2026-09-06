"""The two rankings are one endpoint each, ordered by a query parameter.

Each ranking used to be two paths that differed only in which ``order``
they passed — and one of the four, ``recent-check-ins``, had no caller at
all. Collapsing them moved the choice onto the wire, so what needs
pinning is that the wire value actually reaches the service: nothing else
in the suite exercises these endpoints over HTTP.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import stats as admin_stats
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.staff import Staff, StaffStatus
from app.services.check_in import CheckInActivity, CheckInRankOrder
from app.services.credits.analytics import UserActivity, UserActivityOrder

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


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
def spend_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake(db, *, days, limit, order):
        calls.append({"days": days, "limit": limit, "order": order})
        return [
            UserActivity(
                user_id=1,
                username="axon_1",
                name="Ann",
                spend_micro=15_000_000,
                settled_turns=3,
                balance_micro=50_000_000,
                last_spend_at=_NOW,
            )
        ]

    monkeypatch.setattr(admin_stats.analytics, "user_activity", fake)
    return calls


@pytest.fixture
def check_in_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake(db, *, since, limit, order):
        calls.append({"since": since, "limit": limit, "order": order})
        return [
            CheckInActivity(
                user_id=1,
                username="axon_1",
                name="Ann",
                check_ins=4,
                streak_day=2,
                last_check_in_at=_NOW,
            )
        ]

    monkeypatch.setattr(admin_stats.check_in, "user_activity", fake)
    return calls


def test_the_spend_ranking_defaults_to_the_leaderboard(spend_calls) -> None:
    """``SPEND`` is the default because the dashboard's headline ranking
    is the leaderboard; a caller that omits ``order`` gets what the old
    ``top-spenders`` path returned."""
    body = _client().get("/stats/user-activity?days=7").json()

    assert len(spend_calls) == 1
    assert spend_calls[0]["days"] == 7
    assert spend_calls[0]["limit"] == 20
    # ``is``, not ``==``: ``UserActivityOrder`` is a ``StrEnum``, so the
    # member compares equal to its own value and ``==`` would pass on a
    # bare ``"spend"`` — which is exactly the coercion this file exists
    # to pin.
    assert spend_calls[0]["order"] is UserActivityOrder.SPEND
    assert body[0]["spend_credits"] == 1


def test_the_spend_ranking_takes_its_ordering_from_the_query(spend_calls) -> None:
    _client().get("/stats/user-activity?days=7&order=recent&limit=5")

    assert len(spend_calls) == 1
    assert spend_calls[0]["limit"] == 5
    assert spend_calls[0]["order"] is UserActivityOrder.RECENT


def test_the_check_in_ranking_defaults_to_days_claimed(check_in_calls) -> None:
    body = _client().get("/stats/check-in-activity?days=7").json()

    assert check_in_calls[0]["limit"] == 20
    assert check_in_calls[0]["order"] is CheckInRankOrder.COUNT
    assert body[0]["check_ins"] == 4


def test_the_check_in_ranking_takes_its_ordering_from_the_query(check_in_calls) -> None:
    _client().get("/stats/check-in-activity?days=7&order=recent")

    assert check_in_calls[0]["order"] is CheckInRankOrder.RECENT


def test_an_ordering_the_ranking_does_not_have_is_refused(spend_calls) -> None:
    """422 rather than a silent fallback to the default: an operator
    asking for an ordering that does not exist is a bug in the caller,
    and quietly answering with a different one hides it."""
    assert _client().get("/stats/user-activity?days=7&order=alphabetical").status_code == 422
    assert spend_calls == []
