"""Official Connector catalog: instantiation rules (no DB required)."""

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors import ConnectorCatalogEntryNotFoundError
from app.models.mcp_server import MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.schemas.mcp_server import ConnectorCatalogEntryResponse
from app.services.mcp import catalog
from app.services.mcp.catalog import INSTANTIABLE_AUTH_TYPES, build_connector


def _entry(**over) -> MCPServerCatalogEntry:
    base = dict(
        id=7, key="notion", name="Notion", url="https://mcp.notion.com/mcp",
        auth_type=MCPServerAuthType.OAUTH, oauth_scope="read",
        description="Search your pages", icon_url=None,
        published=True, sort_order=0,
    )
    base.update(over)
    return MCPServerCatalogEntry(**base)


class TestBuildConnector:
    def test_copies_the_template_and_links_back(self):
        connector = build_connector(_entry(), user_id=42)

        assert connector.user_id == 42
        assert connector.catalog_id == 7
        assert connector.name == "Notion"
        assert connector.url == "https://mcp.notion.com/mcp"
        assert connector.auth_type == MCPServerAuthType.OAUTH
        assert connector.oauth_scope == "read"

    def test_oauth_starts_disconnected_until_authorized(self):
        # Same rule as a hand-added OAuth Connector: no tokens yet, so it must
        # not be handed to the agent.
        assert build_connector(_entry(), user_id=1).connected is False

    def test_non_oauth_starts_connected(self):
        entry = _entry(auth_type=MCPServerAuthType.NONE, oauth_scope=None)
        assert build_connector(entry, user_id=1).connected is True

    @pytest.mark.parametrize(
        "auth_type", [MCPServerAuthType.BEARER, MCPServerAuthType.HEADER],
    )
    def test_rejects_auth_types_needing_a_per_user_secret(self, auth_type):
        # The catalog holds no credential and the add flow has nowhere to ask
        # for one, so such an entry would yield a connector that probes fine and
        # then 401s on every real tool call.
        with pytest.raises(ConnectorCatalogEntryNotFoundError):
            build_connector(_entry(auth_type=auth_type), user_id=1)

    def test_carries_no_credentials(self):
        # The catalog never holds secrets — the OAuth client is registered at
        # add time and tokens arrive from the authorize flow.
        connector = build_connector(_entry(), user_id=1)
        assert connector.encrypted_credential is None
        assert connector.oauth_client_encrypted is None
        assert connector.oauth_tokens_encrypted is None


class TestUnpublishedEntriesAreUnreachable:
    """Unpublishing is the operator's off switch, and it has to hold on both
    reads. They are not the same promise: the listing merely stops advertising,
    while the fetch-by-id is the one an attacker reaches with an id kept from an
    earlier listing."""

    async def _statement(self, call) -> str:
        db = AsyncMock()
        # ``Result``'s own accessors are sync; an AsyncMock would hand back
        # coroutines and the caller would fail on the double, not the query.
        db.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=iter(())),
            scalar_one_or_none=MagicMock(return_value=None),
        )
        with contextlib.suppress(ConnectorCatalogEntryNotFoundError):
            await call(db)
        return " ".join(str(db.execute.await_args.args[0].compile()).split())

    async def test_the_listing_offers_only_published_entries(self):
        sql = await self._statement(catalog.list_published)

        assert "mcp_server_catalog.published IS true" in sql

    async def test_an_id_kept_from_an_earlier_listing_stops_working(self):
        # Without this predicate an unpublished entry stays addable by anyone
        # who wrote its id down, which is the whole thing unpublishing prevents.
        sql = await self._statement(
            lambda db: catalog.get_published_entry(db, 7)
        )

        assert "mcp_server_catalog.published IS true" in sql
        assert "mcp_server_catalog.id = " in sql


class TestInstantiableAuthTypes:
    def test_only_the_credential_free_ones(self):
        # The operator CLI's --auth-type choices are derived from this set, so
        # widening it must be a deliberate edit, not a side effect.
        assert INSTANTIABLE_AUTH_TYPES == {
            MCPServerAuthType.NONE, MCPServerAuthType.OAUTH,
        }


class TestCatalogEntryResponse:
    @pytest.mark.parametrize("field", ["published", "sort_order"])
    def test_operator_fields_stay_server_side(self, field):
        assert field not in ConnectorCatalogEntryResponse.model_fields

    def test_exposes_what_the_directory_renders(self):
        response = ConnectorCatalogEntryResponse.model_validate(_entry())
        assert response.key == "notion"
        assert response.name == "Notion"
        assert response.description == "Search your pages"
