"""No pooled connection is held across a third-party round-trip.

Every request opens its transaction at the *auth* dependency's own SELECT,
before a handler body runs, and holds that pooled connection until something
commits. A probe or an OAuth discovery in between leaves it
``idle in transaction`` — checked out, doing nothing, for as long as somebody
else's server takes. Enough of those and the pool is empty, and then requests
that never touch a Connector start failing too.

These pin the two halves of the fix: the release happens before every outbound
call, and it happens with nothing pending — because a commit over a mutation
would strand it when the probe that follows fails.
"""

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from app.api.public import mcp_server as endpoint
from app.database import release_connection
from app.errors import ConnectorNotFoundError
from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.schemas.mcp_server import ConnectorUpdate
from app.services.mcp import authorization

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _session() -> AsyncMock:
    db = AsyncMock()
    db.new, db.dirty, db.deleted = set(), set(), set()
    return db


class TestTheReleaseIsRefusedOverPendingWork:
    async def test_a_pending_change_is_refused_rather_than_committed(self):
        # The failure this exists to prevent is silent: a mutation committed by
        # the release, then stranded by a probe that raises. Without the commit
        # the same exception leaves the row untouched. A ``RuntimeError``, not
        # an ``AssertionError`` — the guard must survive ``python -O``.
        db = _session()
        db.dirty = {MagicMock()}

        with pytest.raises(RuntimeError, match="pending change"):
            await release_connection(db)

        db.commit.assert_not_awaited()

    async def test_a_read_only_prelude_is_released(self):
        db = _session()

        await release_connection(db)

        db.commit.assert_awaited_once()


class TestEveryOutboundCallIsPrecededByARelease:
    """Source-level, and deliberately so: the property is "no *future* handler
    forgets", which no behavioural test of today's handlers can express."""

    @pytest.mark.parametrize(
        "module,function,call",
        [
            (endpoint, "create_connector", "await probe("),
            (endpoint, "_build_catalog_connector", "await authorization.register_client("),
            (endpoint, "_build_oauth_connector", "await authorization.register_client("),
            (authorization, "start", "await oauth.discover_authorization("),
            (authorization, "complete", "await oauth.exchange_code("),
            (endpoint, "create_connector_tool_listing", "await probe("),
            (endpoint, "update_connector", "await probe("),
        ],
    )
    def test_the_release_comes_first(self, module, function, call):
        source = inspect.getsource(getattr(module, function))
        assert "release_connection(db)" in source, f"{function} holds its connection"
        assert source.index("release_connection(db)") < source.index(call), (
            f"{function} dials before releasing"
        )


class TestEveryProbeOfAnExistingRowIsFollowedByAReread:
    """Source-level for the same reason as the release: what has to hold is
    that no future handler writes to the pre-probe copy."""

    @pytest.mark.parametrize(
        "function",
        ["update_connector", "create_connector_tool_listing", "_probe_after_authorization"],
    )
    def test_the_row_is_read_again_after_the_probe(self, function):
        source = inspect.getsource(getattr(endpoint, function))
        assert source.index("await probe(") < source.rindex("await _get_owned_connector("), (
            f"{function} writes to the row it read before the probe"
        )


class TestUpdateProbesTheRowItIsAboutToBecome:
    """The subtlety the restructure had to preserve: what gets probed is decided
    by the row *after* the change, and the probe now runs before the change."""

    @pytest.fixture(autouse=True)
    def _encryption_key(self, monkeypatch):
        from app.services import crypto

        monkeypatch.setattr(
            crypto.settings,
            "mcp_credential_encryption_key",
            Fernet.generate_key().decode(),
        )
        crypto._cipher.cache_clear()
        yield
        crypto._cipher.cache_clear()

    def _connector(self, **over):
        base = dict(
            id=1,
            user_id=7,
            name="c",
            url="https://mcp.example.com/mcp",
            auth_type=MCPServerAuthType.OAUTH,
            connected=True,
            oauth_tokens_encrypted="sealed",
            disabled_tools=[],
            # Columns the response model requires and only a DB default fills.
            oauth_reauthorization_required=False,
            last_connect_failed=False,
            resources_enabled=False,
            created_at=_EPOCH,
            updated_at=_EPOCH,
        )
        base.update(over)
        return MCPServer(**base)

    async def _patch(self, monkeypatch, connector, body, probe):
        monkeypatch.setattr(endpoint, "probe", probe)
        monkeypatch.setattr(
            endpoint,
            "_get_owned_connector",
            AsyncMock(return_value=connector),
        )
        monkeypatch.setattr(endpoint, "_commit_or_conflict", AsyncMock())
        # The URL guard has its own cover in test_connector_url_uniqueness;
        # here it would only answer from a double that knows nothing.
        monkeypatch.setattr(endpoint, "_reject_duplicate_url", AsyncMock())
        monkeypatch.setattr(endpoint.authorization, "release", AsyncMock())
        monkeypatch.setattr(endpoint, "_decrypt_or_none", lambda ciphertext: None)
        monkeypatch.setattr(endpoint.limiter, "enabled", False)
        return await endpoint.update_connector(
            request=MagicMock(),
            connector_id=1,
            body=body,
            background_tasks=MagicMock(),
            db=_session(),
            redis=AsyncMock(),
            current_user=MagicMock(id=7),
        )

    async def test_leaving_oauth_for_bearer_is_probed(self, monkeypatch):
        # Reading ``connector.auth_type`` before the switch says OAUTH and skips
        # the probe — the row would be saved as BEARER without ever being
        # checked, which is the one thing create refuses to allow.
        probed = []

        async def probe(url, auth_type, credential):
            probed.append((url, auth_type, credential))
            return endpoint.ProbeResult(server_name="s", tools=[], icons=[])

        await self._patch(
            monkeypatch,
            self._connector(),
            ConnectorUpdate(auth_type=MCPServerAuthType.BEARER, credential="tok"),
            probe,
        )

        assert probed == [
            ("https://mcp.example.com/mcp", MCPServerAuthType.BEARER, "tok"),
        ]

    async def test_a_failed_probe_writes_nothing(self, monkeypatch):
        # What the release must not break. The row keeps its old name even
        # though the rename was asked for in the same PATCH.
        connector = self._connector(auth_type=MCPServerAuthType.BEARER, name="before")

        async def probe(url, auth_type, credential):
            raise RuntimeError("unreachable")

        with pytest.raises(RuntimeError):
            await self._patch(
                monkeypatch,
                connector,
                ConnectorUpdate(name="after", url="https://other.example.com/mcp"),
                probe,
            )

        assert connector.name == "before"
        assert connector.url == "https://mcp.example.com/mcp"


class TestARowDeletedDuringTheProbeIsNotFound:
    """The other side of releasing: while the probe runs, nothing holds the
    row, so a concurrent DELETE can land. Writing to the pre-probe copy would
    match no row and surface as a ``StaleDataError`` 500; re-reading after the
    probe turns it into the 404 the client already handles."""

    async def test_a_tool_listing_answers_not_found(self, monkeypatch):
        connector = MCPServer(id=1, user_id=7, url="https://mcp.example.com/mcp")
        monkeypatch.setattr(
            endpoint,
            "_get_owned_connector",
            AsyncMock(side_effect=[connector, ConnectorNotFoundError("gone")]),
        )
        monkeypatch.setattr(
            endpoint, "_probe_credential", lambda _c: (MCPServerAuthType.NONE, None)
        )
        monkeypatch.setattr(
            endpoint,
            "probe",
            AsyncMock(return_value=endpoint.ProbeResult(server_name="s", tools=[])),
        )
        monkeypatch.setattr(endpoint.limiter, "enabled", False)
        db = _session()

        with pytest.raises(ConnectorNotFoundError):
            await endpoint.create_connector_tool_listing(
                request=MagicMock(), connector_id=1, db=db, current_user=MagicMock(id=7)
            )

        # The release's commit, and no other.
        db.commit.assert_awaited_once()
        assert connector.tool_count is None
