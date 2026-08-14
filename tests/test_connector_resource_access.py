"""The switch governing whether the agent may read a Connector's resources.

MCP has two halves. ``disabled_tools`` governs the tools a server *publishes*,
matched against the bare names it publishes them under. The resources half
reaches the agent as three tools the host synthesizes under a prefix outside
that namespace, so no entry in ``disabled_tools`` can name one — which is what
``resources_enabled`` exists for, and why the two are separate columns.

Everything here is about one property: **only the user turns it on.** Not a
create body, not a probe, not a PATCH that was about something else.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fastapi import BackgroundTasks

from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.schemas.mcp_server import ConnectorCreate, ConnectorResponse, ConnectorUpdate

_NOW = datetime(2026, 8, 14, tzinfo=UTC)


def _connector(**over) -> MCPServer:
    """A stored Connector with a static credential, as loaded for a request.

    Left unsaved on purpose: the endpoint mutates the instance, and every
    assertion here is about that instance rather than a serialized copy of it.
    """
    base = dict(
        id=1,
        user_id=7,
        name="c",
        url="https://mcp.example.com/mcp",
        auth_type=MCPServerAuthType.NONE,
        disabled_tools=[],
        advertised_icons=[],
        tools_preview=[],
        tool_count=0,
        resources_enabled=False,
        connected=True,
        oauth_reauthorization_required=False,
        last_connect_failed=False,
        # Server defaults, so an unsaved instance has neither, and the response
        # model requires both.
        created_at=_NOW,
        updated_at=_NOW,
    )
    base.update(over)
    return MCPServer(**base)


def _db() -> AsyncMock:
    """A session double that is honest about holding no pending writes.

    An ``AsyncMock`` invents truthy attributes on demand, so a bare one claims
    to hold pending inserts, updates *and* deletes — which is what a session
    that has only read looks nothing like. Same double as
    ``test_connector_oauth._session``.
    """
    db = AsyncMock()
    db.new, db.dirty, db.deleted = set(), set(), set()
    return db


async def _patch(monkeypatch, connector: MCPServer, body: ConnectorUpdate) -> None:
    """Run ``update_connector`` against *connector* with only I/O stubbed."""
    from app.api.public import mcp_server as endpoint
    from app.rate_limit import limiter
    from app.services.mcp.probe import ProbeResult

    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(endpoint, "_reject_duplicate_url", AsyncMock())
    # A URL change probes the row as it will be, which resolves a hostname and
    # opens a connection. Neither is what any test here is about.
    monkeypatch.setattr(endpoint, "_release_connection", AsyncMock())
    monkeypatch.setattr(
        endpoint,
        "probe",
        AsyncMock(return_value=ProbeResult(server_name="s", tools=[])),
    )
    monkeypatch.setattr(endpoint, "_get_owned_connector", AsyncMock(return_value=connector))
    monkeypatch.setattr(endpoint, "_commit_or_conflict", AsyncMock())
    monkeypatch.setattr(endpoint, "open_secret", lambda sealed, **_: sealed)

    await endpoint.update_connector(
        request=MagicMock(),
        connector_id=1,
        body=body,
        background_tasks=BackgroundTasks(),
        db=_db(),
        redis=AsyncMock(),
        current_user=MagicMock(id=7),
    )


class TestOffUntilAsked:
    def test_a_row_that_predates_the_column_lands_off(self):
        # The server default is what answers for those rows, and
        # ``b6e3d9a70f52`` adds the column with it — so an upgrade does not
        # widen anyone's data access without them asking. NOT NULL is the other
        # half: no row can arrive carrying no answer at all.
        column = MCPServer.__table__.c.resources_enabled
        assert column.server_default.arg.text == "false"
        assert column.nullable is False

    def test_creating_a_connector_cannot_turn_it_on(self):
        # Not merely defaulted off — unsettable at create, so turning it on is
        # always a second, deliberate act.
        assert "resources_enabled" not in ConnectorCreate.model_fields

        # A body that names it anyway is ignored rather than refused, which is
        # pydantic's default here and enough: what matters is that nothing the
        # body says reaches the row.
        created = ConnectorCreate.model_validate(
            {
                "name": "gh",
                "url": "https://mcp.gh.dev/x",
                "resources_enabled": True,
            }
        )

        assert not hasattr(created, "resources_enabled")


class TestTheUserTurnsItOn:
    async def test_a_patch_turns_it_on(self, monkeypatch):
        connector = _connector()

        await _patch(monkeypatch, connector, ConnectorUpdate(resources_enabled=True))

        assert connector.resources_enabled is True

    async def test_a_patch_turns_it_off_again(self, monkeypatch):
        connector = _connector(resources_enabled=True)

        await _patch(monkeypatch, connector, ConnectorUpdate(resources_enabled=False))

        assert connector.resources_enabled is False

    async def test_a_patch_about_something_else_leaves_it_alone(self, monkeypatch):
        # ``False`` and "not mentioned" are different requests, and the endpoint
        # tells them apart by ``None`` rather than by falsiness.
        connector = _connector(resources_enabled=True)

        await _patch(monkeypatch, connector, ConnectorUpdate(name="renamed"))

        assert (connector.name, connector.resources_enabled) == ("renamed", True)


class TestRepointingTheConnectorRevokesIt:
    """The grant is about a server, and the URL is which server this is."""

    async def test_a_url_change_switches_it_off(self, monkeypatch):
        # Carrying it across would hand a server the user has never considered
        # read access to everything it holds, on a decision they made about a
        # different one. Only non-OAuth rows reach here — repointing an OAuth
        # Connector is refused outright, because its token is bound to the URL.
        connector = _connector(resources_enabled=True)

        await _patch(
            monkeypatch,
            connector,
            ConnectorUpdate(url="https://elsewhere.example.com/mcp"),
        )

        assert connector.url == "https://elsewhere.example.com/mcp"
        assert connector.resources_enabled is False

    async def test_a_patch_that_changes_nothing_else_leaves_it_on(self, monkeypatch):
        # The reset is the URL's doing, not every PATCH's — otherwise renaming a
        # Connector would quietly revoke access nobody asked to revoke.
        connector = _connector(resources_enabled=True)

        await _patch(monkeypatch, connector, ConnectorUpdate(name="renamed"))

        assert connector.resources_enabled is True


class TestNothingElseWritesIt:
    def test_a_probe_does_not(self):
        # ``_apply_probe`` copies what a probe *learned*; this is what the user
        # *decided*. Re-listing a server's tools — which the Connectors page
        # offers as a button — must not quietly re-grant access somebody had
        # switched off, and there are five probe paths that would.
        from app.api.public.mcp_server import _apply_probe
        from app.services.mcp.probe import ProbeResult

        connector = _connector(resources_enabled=True)

        _apply_probe(connector, ProbeResult(server_name="s", tools=[{"name": "t"}]))

        assert connector.resources_enabled is True

    def test_the_response_reports_it(self):
        # Same name as the column, like its neighbours: ``model_validate`` maps
        # by attribute, so a renamed field here would serialize as ``False``
        # for a Connector the user had switched on.
        connector = _connector(resources_enabled=True)

        assert ConnectorResponse.model_validate(connector).resources_enabled is True
