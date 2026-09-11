"""Cancelling a turn that already stopped is a no-op, not an error.

The stop button is driven by the live SSE stream, and that stream can die
in a way the browser never surfaces — a suspend, a Wi-Fi handover, an
idle timeout at the load balancer. The button then outlives the turn, and
the click that follows used to get a 409 the UI rendered as a red error:
a reported failure for a request whose outcome — this turn not running —
already held.

Nothing may be published for a terminal session either. There is no
worker listening on the cancel channel, and a stray publish on a session
that has since started a *new* turn would cancel that one instead.

Accepting quietly is only half of it: the caller's running state is
normally released by the ``agent.stopped`` a signalled worker sends, so a
request that signalled nobody has to say ``already_stopped`` or the
client waits forever for an event that is not coming.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_db, get_redis
from app.api.public import session as session_api
from app.auth import authenticate_and_get_user
from app.exception_handlers import install_error_handlers
from app.models.session import Session, SessionStatus
from app.models.user import User
from app.rate_limit import limiter

SESSION_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    # The limiter counts in Redis and has its own tests; here it would
    # only turn a second call into a 429.
    monkeypatch.setattr(limiter, "enabled", False)


def _app(monkeypatch, status: SessionStatus, redis) -> TestClient:
    """A client whose one session sits at *status*.

    ``_get_or_404`` is a module-level helper rather than a dependency, so
    it is patched rather than overridden; ``monkeypatch`` restores it.
    """
    row = Session(id=SESSION_ID, user_id=7, status=status)

    async def _found(_db, _session_id, _user_id, **_kw):
        return row

    monkeypatch.setattr(session_api, "_get_or_404", _found)
    # No turn is ever PENDING here: the terminal cases have no turn at
    # all, and the RUNNING case is the claimed one that takes pub/sub.
    monkeypatch.setattr(
        session_api,
        "cancel_unclaimed_turn",
        AsyncMock(return_value=False),
    )

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(session_api.router)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[authenticate_and_get_user] = lambda: User(id=7)
    return TestClient(app)


@pytest.mark.parametrize(
    "status",
    [SessionStatus.COMPLETE, SessionStatus.CANCELLED, SessionStatus.FAILED, SessionStatus.IDLE],
)
def test_cancelling_a_stopped_session_is_accepted_silently(status, monkeypatch):
    """Every non-RUNNING status answers 202 and publishes nothing.

    ``IDLE`` is in here with the three terminals on purpose: a session
    that never ran has just as little to stop, and the caller asked for
    the same outcome.
    """
    redis = AsyncMock()

    response = _app(monkeypatch, status, redis).post(f"/sessions/{SESSION_ID}/cancel")

    assert response.status_code == 202
    assert response.json() == {"already_stopped": True}
    redis.publish.assert_not_awaited()


def test_an_unclaimed_turn_is_hard_cancelled_and_says_already_stopped(monkeypatch):
    """A ``PENDING`` turn is cancelled in the DB with no worker involved,
    so no ``agent.stopped`` follows this one either — same flag as a
    terminal session, for the same reason."""
    redis = AsyncMock()
    client = _app(monkeypatch, SessionStatus.RUNNING, redis)
    monkeypatch.setattr(
        session_api,
        "cancel_unclaimed_turn",
        AsyncMock(return_value=True),
    )

    response = client.post(f"/sessions/{SESSION_ID}/cancel")

    assert response.status_code == 202
    assert response.json() == {"already_stopped": True}
    redis.publish.assert_not_awaited()


def test_cancelling_a_running_session_still_signals_the_worker(monkeypatch):
    """The regression guard: making the terminal case quiet must not make
    the real case quiet too."""
    redis = AsyncMock()

    response = _app(monkeypatch, SessionStatus.RUNNING, redis).post(
        f"/sessions/{SESSION_ID}/cancel"
    )

    assert response.status_code == 202
    # Not "already stopped": a worker was signalled, so the terminal is
    # coming over the stream and the client must keep waiting for it
    # rather than standing its own state down.
    assert response.json() == {"already_stopped": False}
    redis.publish.assert_awaited_once()
    channel, payload = redis.publish.await_args.args
    assert str(SESSION_ID) in channel
    assert payload == "user_cancelled"
