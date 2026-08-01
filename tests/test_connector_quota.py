"""A user holds at most ``MAX_CONNECTORS_PER_USER`` Connectors.

Not an abuse limit so much as a cost one: ``_resolve_mcp_servers`` follows a
"connected = active" model, so every Connector a user holds and has connected
puts its whole tool catalog into the model's context on *every step* of every
turn. The quota bounds what one account can make a turn cost.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.endpoints import mcp_server as endpoint
from app.errors import ConnectorQuotaExceededError
from app.models.mcp_server import MCPServerAuthType
from app.schemas.mcp_server import MAX_CONNECTORS_PER_USER, ConnectorCreate


def _session(held: int) -> AsyncMock:
    db = AsyncMock()
    db.new, db.dirty, db.deleted = set(), set(), set()
    db.add = MagicMock()   # sync on the real Session; an AsyncMock would only
                           # ever record a coroutine nobody awaits
    db.scalar.return_value = held
    return db


async def _create(db, monkeypatch, probe=None):
    if probe is None:
        async def probe(url, auth_type, credential):
            return endpoint.ProbeResult(server_name="s", tools=[], icons=[])
    monkeypatch.setattr(endpoint, "probe", probe)
    monkeypatch.setattr(endpoint, "_commit_or_conflict", AsyncMock())
    monkeypatch.setattr(endpoint, "_to_response", lambda connector: connector)
    monkeypatch.setattr(endpoint.limiter, "enabled", False)
    return await endpoint.create_connector(
        request=MagicMock(),
        body=ConnectorCreate(
            name="c", url="https://mcp.example.com/mcp",
            auth_type=MCPServerAuthType.NONE,
        ),
        db=db,
        current_user=MagicMock(id=7),
    )


class TestTheQuotaIsEnforced:
    async def test_a_user_at_the_limit_is_refused(self, monkeypatch):
        db = _session(held=MAX_CONNECTORS_PER_USER)

        with pytest.raises(ConnectorQuotaExceededError) as caught:
            await _create(db, monkeypatch)

        assert caught.value.status_code == 409
        db.add.assert_not_called()

    async def test_one_below_the_limit_still_creates(self, monkeypatch):
        db = _session(held=MAX_CONNECTORS_PER_USER - 1)

        await _create(db, monkeypatch)

        db.add.assert_called_once()

    async def test_the_count_is_scoped_to_the_caller(self, monkeypatch):
        # A quota counted over the whole table would let one heavy user lock
        # every other account out of creating anything.
        db = _session(held=0)

        await _create(db, monkeypatch)

        where = str(db.scalar.await_args.args[0])
        assert "mcp_server.user_id = " in where
        assert "connected" not in where, (
            "the quota counts rows held, not rows connected — a disconnected "
            "Connector still occupies one"
        )


class TestTheRefusalCostsNothing:
    async def test_no_server_is_dialled_for_a_create_that_cannot_land(
        self, monkeypatch,
    ):
        # The check precedes the probe deliberately: refusing after the
        # round-trip would let a capped account keep making the backend dial
        # arbitrary hosts, which is the SSRF surface paid for nothing.
        dialled = []

        async def probe(url, auth_type, credential):
            dialled.append(url)
            return endpoint.ProbeResult(server_name="s", tools=[], icons=[])

        with pytest.raises(ConnectorQuotaExceededError):
            await _create(_session(held=MAX_CONNECTORS_PER_USER), monkeypatch, probe)

        assert dialled == []
