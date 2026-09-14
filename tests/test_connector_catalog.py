"""Official Connector catalog: instantiation rules (no DB required)."""

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from app.api.public import mcp_server as endpoint
from app.errors import ConnectorCatalogCredentialError, ConnectorCatalogEntryNotFoundError
from app.models.mcp_server import MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.schemas.mcp_server import ConnectorCatalogEntryResponse
from app.services import crypto
from app.services.localized_text import DEFAULT_LOCALE
from app.services.mcp import catalog
from app.services.mcp.catalog import (
    CREDENTIAL_AUTH_TYPES,
    INSTANTIABLE_AUTH_TYPES,
    build_connector,
)


def _entry(**over) -> MCPServerCatalogEntry:
    base = dict(
        id=7,
        name="Notion",
        url="https://mcp.notion.com/mcp",
        auth_type=MCPServerAuthType.OAUTH,
        oauth_scope="read",
        description="Search your pages",
        icon_url=None,
        published=True,
        sort_order=0,
    )
    base.update(over)
    return MCPServerCatalogEntry(**base)


class TestBuildConnector:
    def test_copies_the_template_and_links_back(self):
        connector = build_connector(_entry(), user_id=42, locale=DEFAULT_LOCALE)

        assert connector.user_id == 42
        assert connector.catalog_id == 7
        assert connector.name == "Notion"
        assert connector.url == "https://mcp.notion.com/mcp"
        assert connector.auth_type == MCPServerAuthType.OAUTH
        assert connector.oauth_scope == "read"

    def test_oauth_starts_disconnected_until_authorized(self):
        # Same rule as a hand-added OAuth Connector: no tokens yet, so it must
        # not be handed to the agent.
        assert build_connector(_entry(), user_id=1, locale=DEFAULT_LOCALE).connected is False

    def test_non_oauth_starts_connected(self):
        entry = _entry(auth_type=MCPServerAuthType.NONE, oauth_scope=None)
        assert build_connector(entry, user_id=1, locale=DEFAULT_LOCALE).connected is True

    def test_rejects_header_auth(self):
        # HEADER needs a template-level header name the catalog has no column
        # for, so it cannot be offered at all.
        with pytest.raises(ConnectorCatalogEntryNotFoundError):
            build_connector(
                _entry(auth_type=MCPServerAuthType.HEADER), user_id=1, locale=DEFAULT_LOCALE
            )

    def test_bearer_requires_the_users_credential(self):
        # A BEARER entry without the user's token would probe fine and then
        # 401 on every real tool call, so it fails at add time instead.
        with pytest.raises(ConnectorCatalogCredentialError):
            build_connector(
                _entry(auth_type=MCPServerAuthType.BEARER, oauth_scope=None),
                user_id=1,
                locale=DEFAULT_LOCALE,
            )

    def test_bearer_carries_the_users_credential_and_starts_connected(self):
        connector = build_connector(
            _entry(auth_type=MCPServerAuthType.BEARER, oauth_scope=None),
            user_id=1,
            locale=DEFAULT_LOCALE,
            encrypted_credential="ciphertext",
        )
        assert connector.encrypted_credential == "ciphertext"
        assert connector.connected is True

    @pytest.mark.parametrize(
        "auth_type",
        [MCPServerAuthType.NONE, MCPServerAuthType.OAUTH],
    )
    def test_credential_free_entries_refuse_a_credential(self, auth_type):
        # Sent to an entry that takes none it is rejected, not silently dropped —
        # the same footgun the create schema guards for the other fields.
        with pytest.raises(ConnectorCatalogCredentialError):
            build_connector(
                _entry(auth_type=auth_type),
                user_id=1,
                locale=DEFAULT_LOCALE,
                encrypted_credential="ciphertext",
            )

    def test_carries_no_credentials(self):
        # The catalog never holds secrets — the OAuth client is registered at
        # add time and tokens arrive from the authorize flow.
        connector = build_connector(_entry(), user_id=1, locale=DEFAULT_LOCALE)
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
        sql = await self._statement(lambda db: catalog.get_published_entry(db, 7))

        assert "mcp_server_catalog.published IS true" in sql
        assert "mcp_server_catalog.id = " in sql


class TestInstantiableAuthTypes:
    def test_what_the_catalog_can_offer(self):
        # The operator CLI's --auth-type choices are derived from this set, so
        # widening it must be a deliberate edit, not a side effect.
        assert INSTANTIABLE_AUTH_TYPES == {
            MCPServerAuthType.NONE,
            MCPServerAuthType.OAUTH,
            MCPServerAuthType.BEARER,
        }

    def test_which_of_those_take_the_users_credential(self):
        assert CREDENTIAL_AUTH_TYPES == {MCPServerAuthType.BEARER}
        assert CREDENTIAL_AUTH_TYPES <= INSTANTIABLE_AUTH_TYPES


class TestWhatIdentifiesAnEntry:
    """One row per server, and ``name`` is only the label on it.

    ``key`` used to carry this table's single unique constraint, which was the
    one thing it did that ``id`` and ``name`` did not. Moving it to ``url``
    keeps the guard and drops the third identity.
    """

    def test_two_entries_cannot_offer_the_same_server(self):
        assert MCPServerCatalogEntry.__table__.c.url.unique is True

    def test_two_entries_may_share_a_display_name(self):
        # Not an oversight: ``name`` is copied onto the user's row and shown
        # to them, and an operator renaming one entry must not fail because
        # another already reads that way.
        assert not MCPServerCatalogEntry.__table__.c.name.unique

    def test_nothing_is_offered_under_a_slug_any_more(self):
        assert "key" not in MCPServerCatalogEntry.__table__.c


class TestCatalogEntryResponse:
    @pytest.mark.parametrize(
        "field",
        [
            "published",
            "sort_order",
            # The pre-registered OAuth app is the platform's, not the caller's
            # to see — least of all the encrypted secret.
            "oauth_client_id",
            "oauth_client_secret_encrypted",
        ],
    )
    def test_operator_fields_stay_server_side(self, field):
        assert field not in ConnectorCatalogEntryResponse.model_fields

    def test_exposes_what_the_directory_renders(self):
        response = ConnectorCatalogEntryResponse.model_validate(_entry())
        assert response.name == "Notion"
        assert response.description == "Search your pages"


class TestPreRegisteredClientPassThrough:
    """The add path hands an entry's own OAuth app to client registration.

    A provider without DCR or CIMD (Figma) can only be a catalog entry if the
    platform's pre-registered app rides along; an entry without one must keep
    the DCR/CIMD path exactly as before, which is why the absent case pins
    ``client_id=None`` rather than merely not caring.
    """

    @pytest.fixture
    def fernet_key(self, monkeypatch):
        monkeypatch.setattr(
            crypto.settings,
            "mcp_credential_encryption_key",
            Fernet.generate_key().decode(),
        )
        crypto._cipher.cache_clear()
        yield
        crypto._cipher.cache_clear()

    async def _add(self, monkeypatch, entry):
        registered: dict = {}

        async def _capture(**kwargs):
            registered.update(kwargs)
            return "sealed-client", {"issuer": "https://as.example.com"}

        monkeypatch.setattr(endpoint.catalog, "get_published_entry", AsyncMock(return_value=entry))
        monkeypatch.setattr(endpoint, "_reject_duplicate_url", AsyncMock())
        monkeypatch.setattr(endpoint, "_release_connection", AsyncMock())
        monkeypatch.setattr(endpoint, "_register_oauth_client", _capture)
        await endpoint._build_catalog_connector(
            MagicMock(), catalog_id=entry.id, user_id=42, locale=DEFAULT_LOCALE
        )
        return registered

    async def test_the_entrys_app_reaches_registration_decrypted(self, monkeypatch, fernet_key):
        entry = _entry(
            oauth_client_id="app-id",
            oauth_client_secret_encrypted=crypto.encrypt_secret("app-secret"),
        )

        registered = await self._add(monkeypatch, entry)

        assert registered["client_id"] == "app-id"
        assert registered["client_secret"] == "app-secret"

    async def test_an_entry_without_an_app_keeps_the_dcr_path(self, monkeypatch):
        registered = await self._add(monkeypatch, _entry())

        assert registered["client_id"] is None
        assert registered["client_secret"] is None
