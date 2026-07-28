"""Unit tests for the Connector OAuth service (pure / no-network parts)."""

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata

from app.errors import ConnectorOAuthError, InvalidConnectorUrlError
from app.services.mcp.oauth import (
    DiscoveredAuthorization,
    _require_pkce_support,
    build_authorization_url,
    cimd_client,
    client_metadata_document,
    discover_authorization,
    ensure_client,
    exchange_code,
    new_pkce,
    new_state,
)


def _metadata() -> OAuthMetadata:
    return OAuthMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        response_types_supported=["code"],
    )


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id="cid", redirect_uris=["https://app/cb"])


class TestBuildAuthorizationUrl:
    def test_includes_pkce_and_state(self):
        url = build_authorization_url(
            _metadata(), _client(),
            server_url="https://mcp.example.com/mcp",
            redirect_uri="https://app/cb", scope="read",
            state="st", pkce=new_pkce(),
        )
        assert url.startswith("https://as.example.com/authorize?")
        assert "response_type=code" in url
        assert "client_id=cid" in url
        assert "state=st" in url
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "scope=read" in url

    def test_omits_scope_when_none(self):
        url = build_authorization_url(
            _metadata(), _client(),
            server_url="https://mcp.example.com/mcp",
            redirect_uri="https://app/cb", scope=None,
            state="st", pkce=new_pkce(),
        )
        assert "scope=" not in url

    def test_appends_with_ampersand_when_endpoint_has_query(self):
        # RFC 6749 §3.1 lets the authorization_endpoint carry its own query; the
        # params must join with '&', not a second '?'.
        metadata = OAuthMetadata(
            issuer="https://as.example.com",
            authorization_endpoint="https://as.example.com/authorize?tenant=acme",
            token_endpoint="https://as.example.com/token",
            response_types_supported=["code"],
        )
        url = build_authorization_url(
            metadata, _client(),
            server_url="https://mcp.example.com/mcp",
            redirect_uri="https://app/cb", scope=None, state="st", pkce=new_pkce(),
        )
        assert url.count("?") == 1
        assert url.startswith("https://as.example.com/authorize?tenant=acme&")
        assert "response_type=code" in url


class TestSpecRequirements:
    """The two MCP MUSTs the browser-redirect legs must satisfy themselves —
    the SDK's ``OAuthClientProvider`` only covers the worker's leg."""

    def test_authorization_carries_the_resource_indicator(self):
        # RFC 8707: binds the token to this server so it can't be replayed.
        url = build_authorization_url(
            _metadata(), _client(),
            server_url="https://mcp.example.com/mcp",
            redirect_uri="https://app/cb", scope=None,
            state="st", pkce=new_pkce(),
        )
        assert "resource=https%3A%2F%2Fmcp.example.com%2Fmcp" in url

    def test_refuses_an_authorization_server_without_pkce(self):
        # Absent ``code_challenge_methods_supported`` means unsupported, and a
        # bare authorization code would be interceptable.
        no_pkce = OAuthMetadata(
            issuer="https://as.example.com",
            authorization_endpoint="https://as.example.com/authorize",
            token_endpoint="https://as.example.com/token",
            response_types_supported=["code"],
        )
        with pytest.raises(ConnectorOAuthError):
            _require_pkce_support(no_pkce)

    def test_accepts_an_authorization_server_advertising_s256(self):
        with_pkce = OAuthMetadata(
            issuer="https://as.example.com",
            authorization_endpoint="https://as.example.com/authorize",
            token_endpoint="https://as.example.com/token",
            response_types_supported=["code"],
            code_challenge_methods_supported=["S256"],
        )
        _require_pkce_support(with_pkce)  # no raise


class TestEnsureClientManual:
    async def test_manual_confidential_client_no_network(self):
        client = await ensure_client(
            _metadata(),
            redirect_uri="https://app/cb", scope="read",
            client_id="cid", client_secret="sek",
        )
        assert client.client_id == "cid"
        assert client.client_secret == "sek"
        assert client.token_endpoint_auth_method == "client_secret_post"

    async def test_manual_public_client_when_no_secret(self):
        client = await ensure_client(
            _metadata(),
            redirect_uri="https://app/cb", scope=None, client_id="cid",
        )
        assert client.token_endpoint_auth_method == "none"


class TestSsrfGuard:
    """Every server-side outbound URL in the OAuth flow is SSRF-guarded — the
    user-supplied server URL and the endpoints derived from server metadata."""

    async def test_discover_authorization_rejects_internal_server_url(self):
        # Guard fires on the private IP before any network request is made.
        with pytest.raises(InvalidConnectorUrlError):
            await discover_authorization("https://10.0.0.1/mcp")

    async def test_exchange_code_rejects_internal_token_endpoint(self):
        # A malicious server can point token_endpoint at an internal host; the
        # guard rejects it before we POST the code + client secret.
        metadata = OAuthMetadata(
            issuer="https://as.example.com",
            authorization_endpoint="https://as.example.com/authorize",
            token_endpoint="https://127.0.0.1/token",
            response_types_supported=["code"],
        )
        with pytest.raises(InvalidConnectorUrlError):
            await exchange_code(
                metadata, _client(),
                code="abc", server_url="https://mcp.example.com/mcp",
                redirect_uri="https://app/cb", code_verifier="v",
            )


class TestBuildOAuthConnector:
    """The row a custom OAuth Connector is created as.

    Regression cover for the create path itself: it builds an ``MCPServer``
    positionally by keyword, so a column rename that misses it raises
    ``TypeError`` at request time while every other test still passes.
    """

    async def test_builds_a_disconnected_row(self, monkeypatch):
        from app.api.endpoints import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType
        from app.schemas.mcp_server import ConnectorCreate

        async def fake_register(**_kwargs):
            return "encrypted-client"

        monkeypatch.setattr(endpoint, "_register_oauth_client", fake_register)

        connector = await endpoint._build_oauth_connector(
            ConnectorCreate(
                name="Notion", url="https://mcp.notion.com/mcp",
                auth_type=MCPServerAuthType.OAUTH, oauth_scope="read",
            ),
            user_id=42,
        )

        assert connector.user_id == 42
        assert connector.auth_type == MCPServerAuthType.OAUTH
        assert connector.oauth_client_encrypted == "encrypted-client"
        # Not handed to the agent until the user comes back from the provider.
        assert connector.connected is False


class TestStateAndPkce:
    def test_state_is_unique(self):
        assert new_state() != new_state()

    def test_pkce_has_verifier_and_challenge(self):
        pkce = new_pkce()
        assert pkce.code_verifier and pkce.code_challenge


class TestClientIdMetadataDocument:
    """CIMD: the client_id is our published metadata URL, so there is no
    registration round-trip and nothing per-Connector to store."""

    def _metadata_supporting_cimd(self) -> OAuthMetadata:
        return OAuthMetadata(
            issuer="https://as.example.com",
            authorization_endpoint="https://as.example.com/authorize",
            token_endpoint="https://as.example.com/token",
            response_types_supported=["code"],
            client_id_metadata_document_supported=True,
        )

    async def test_uses_the_metadata_url_as_client_id_when_advertised(self):
        client = await ensure_client(
            self._metadata_supporting_cimd(),
            redirect_uri="https://app/cb", scope="read",
            client_metadata_url="https://api.example.com/api/connectors/client-metadata",
        )
        assert client.client_id == "https://api.example.com/api/connectors/client-metadata"
        # Public client: the id is a world-readable URL, so there is no secret.
        assert client.client_secret is None
        assert client.token_endpoint_auth_method == "none"

    async def test_falls_back_to_dcr_when_the_server_does_not_advertise_cimd(self):
        # _metadata() omits the flag; DCR would need the network, and the SSRF
        # guard rejects the internal issuer first — proving CIMD was skipped.
        with pytest.raises(InvalidConnectorUrlError):
            await ensure_client(
                OAuthMetadata(
                    issuer="https://10.0.0.1",
                    authorization_endpoint="https://10.0.0.1/authorize",
                    token_endpoint="https://10.0.0.1/token",
                    response_types_supported=["code"],
                    registration_endpoint="https://10.0.0.1/register",
                ),
                redirect_uri="https://app/cb", scope=None,
                client_metadata_url="https://api.example.com/api/connectors/client-metadata",
            )

    async def test_a_manual_client_id_still_wins_over_cimd(self):
        # An operator who configured a real OAuth app must keep using it.
        client = await ensure_client(
            self._metadata_supporting_cimd(),
            redirect_uri="https://app/cb", scope=None,
            client_id="manual-cid", client_secret="sek",
            client_metadata_url="https://api.example.com/api/connectors/client-metadata",
        )
        assert client.client_id == "manual-cid"

    async def test_rejects_a_metadata_url_that_cannot_be_a_client_id(self):
        # Must be https with a non-root path, or the AS cannot resolve it.
        for bad in ("http://api.example.com/client-metadata", "https://api.example.com/"):
            with pytest.raises(ConnectorOAuthError):
                await ensure_client(
                    self._metadata_supporting_cimd(),
                    redirect_uri="https://app/cb", scope=None,
                    client_metadata_url=bad,
                )

    async def test_a_malformed_url_does_not_break_servers_that_use_dcr(self):
        # CIMD is optional; a bad setting must only fail the servers that would
        # have used it, never take down every OAuth add on the deploy.
        with pytest.raises(InvalidConnectorUrlError):
            await ensure_client(
                OAuthMetadata(
                    issuer="https://10.0.0.1",
                    authorization_endpoint="https://10.0.0.1/authorize",
                    token_endpoint="https://10.0.0.1/token",
                    response_types_supported=["code"],
                    registration_endpoint="https://10.0.0.1/register",
                ),
                redirect_uri="https://app/cb", scope=None,
                client_metadata_url="http://not-https/root",
            )

    def test_document_echoes_its_own_url_as_client_id(self):
        # Binding the id to the URL is what stops the document being
        # republished elsewhere under a different identity.
        url = "https://api.example.com/api/connectors/client-metadata"
        doc = client_metadata_document(url, redirect_uri="https://app/cb")
        assert doc["client_id"] == url
        assert doc["redirect_uris"] == ["https://app/cb"]
        assert doc["token_endpoint_auth_method"] == "none"
        assert "client_secret" not in doc

    async def test_published_document_describes_the_client_we_authorize_with(self):
        # The security property of CIMD: what a server fetches must be the
        # identity we actually use. Both come from one builder, so this holds
        # by construction — assert it so a future split can't break it quietly.
        url = "https://api.example.com/api/connectors/client-metadata"
        authorized = await ensure_client(
            self._metadata_supporting_cimd(),
            redirect_uri="https://app/cb", scope="read", client_metadata_url=url,
        )
        published = client_metadata_document(url, redirect_uri="https://app/cb")
        assert published == authorized.model_dump(mode="json", exclude_none=True)
        assert authorized == cimd_client(url, redirect_uri="https://app/cb")


class TestClientMetadataEndpoint:
    """The document is served at the very URL it names as ``client_id``."""

    async def _get(self, monkeypatch, *, metadata_url: str, redirect_uri: str):
        from app.api.endpoints import mcp_server as endpoint

        monkeypatch.setattr(
            endpoint.settings, "mcp_client_metadata_url", metadata_url
        )
        monkeypatch.setattr(endpoint.settings, "mcp_oauth_redirect_uri", redirect_uri)
        return await endpoint.get_client_metadata()

    async def test_serves_the_document_when_configured(self, monkeypatch):
        url = "https://api.example.com/api/connectors/client-metadata"
        doc = await self._get(
            monkeypatch, metadata_url=url, redirect_uri="https://app/cb",
        )
        assert doc["client_id"] == url
        assert doc["redirect_uris"] == ["https://app/cb"]

    async def test_404s_when_cimd_is_not_configured(self, monkeypatch):
        # Publishing an identity that names a redirect URI we may not serve is
        # worse than publishing nothing.
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as caught:
            await self._get(monkeypatch, metadata_url="", redirect_uri="https://app/cb")
        assert caught.value.status_code == 404

    async def test_fails_loudly_when_the_redirect_uri_is_missing(self, monkeypatch):
        # A document advertising no redirect URI would strand every flow.
        with pytest.raises(ConnectorOAuthError):
            await self._get(
                monkeypatch,
                metadata_url="https://api.example.com/api/connectors/client-metadata",
                redirect_uri="",
            )


class TestScopeSelectionStrategy:
    """MCP's scope-selection strategy: a general-purpose client has no domain
    knowledge to pick scopes with, so it asks for what the server declares."""

    def _discovered(self, scopes):
        return DiscoveredAuthorization(metadata=_metadata(), scopes_supported=scopes)

    def test_uses_the_scopes_the_server_declares(self):
        assert self._discovered(["files:read", "files:write"]).scope_for(None) == (
            "files:read files:write"
        )

    def test_a_configured_scope_wins(self):
        # An operator's explicit choice is not a guess, so it outranks discovery.
        assert self._discovered(["files:read"]).scope_for("custom:scope") == "custom:scope"

    def test_omits_scope_when_neither_is_available(self):
        # Omitting lets the server apply its own default rather than us
        # inventing a scope string it may reject.
        assert self._discovered(None).scope_for(None) is None
        assert self._discovered([]).scope_for(None) is None


class TestResponseMapping:
    """``_to_response`` builds via ``model_validate``, which maps by attribute
    name — so a schema field renamed away from its column serializes as the
    default instead of failing, and the UI silently loses the state."""

    def test_reauthorization_flag_survives_the_orm_to_schema_hop(self):
        from datetime import datetime

        from app.api.endpoints.mcp_server import _to_response
        from app.models.mcp_server import MCPServer, MCPServerAuthType

        now = datetime(2026, 7, 28)
        connector = MCPServer(
            id=1, user_id=7, name="Notion", url="https://mcp.notion.com/mcp",
            auth_type=MCPServerAuthType.OAUTH,
            oauth_tokens_encrypted="enc", connected=True,
            oauth_reauthorization_required=True,
            # NOT NULL columns: a loaded row always has these, but SQLAlchemy
            # applies its Python-side defaults at flush, not at construction.
            tool_count=0, disabled_tools=[], created_at=now, updated_at=now,
            advertised_icon_url="https://cdn.example.com/notion.png",
        )
        response = _to_response(connector)

        assert response.oauth_reauthorization_required is True
        assert response.disabled_tools == []
        assert response.advertised_icon_url == "https://cdn.example.com/notion.png"
        assert response.authorized is True  # tokens present, but the grant is dead


class TestProbeCredential:
    """How a saved Connector is authenticated when re-listing its tools."""

    def _connector(self, **over):
        from app.models.mcp_server import MCPServer, MCPServerAuthType

        base = dict(
            id=1, user_id=7, name="c", url="https://mcp.example.com/mcp",
            auth_type=MCPServerAuthType.NONE,
        )
        base.update(over)
        return MCPServer(**base)

    def test_non_oauth_uses_its_own_decrypted_credential(self, monkeypatch):
        from app.api.endpoints import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType

        monkeypatch.setattr(endpoint, "_decrypt_or_none", lambda ciphertext: "tok")
        auth_type, credential = endpoint._probe_credential(
            self._connector(auth_type=MCPServerAuthType.BEARER, encrypted_credential="e")
        )
        assert (auth_type, credential) == (MCPServerAuthType.BEARER, "tok")

    def test_oauth_probes_as_a_bearer_carrying_the_access_token(self, monkeypatch):
        # The MCP server takes the token the same way either route, and
        # ``probe`` has no OAuth path of its own — same trick the callback uses.
        from mcp.shared.auth import OAuthToken

        from app.api.endpoints import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType

        tokens = OAuthToken(access_token="at", token_type="Bearer")
        monkeypatch.setattr(
            endpoint, "decrypt_secret", lambda ciphertext: tokens.model_dump_json()
        )
        auth_type, credential = endpoint._probe_credential(
            self._connector(
                auth_type=MCPServerAuthType.OAUTH, oauth_tokens_encrypted="enc"
            )
        )
        assert (auth_type, credential) == (MCPServerAuthType.BEARER, "at")

    def test_an_unauthorized_oauth_connector_refuses_rather_than_probing_bare(self):
        # Probing with no token would be indistinguishable from an
        # unauthenticated probe and would report "no tools" misleadingly.
        from app.api.endpoints import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType

        with pytest.raises(ConnectorOAuthError):
            endpoint._probe_credential(
                self._connector(
                    auth_type=MCPServerAuthType.OAUTH, oauth_tokens_encrypted=None
                )
            )


class TestDisabledToolsBounds:
    """``disabled_tools`` lands in a JSONB column, so the payload is bounded
    like every other writable field on ``ConnectorUpdate``."""

    def test_accepts_an_ordinary_selection(self):
        from app.schemas.mcp_server import ConnectorUpdate

        body = ConnectorUpdate(disabled_tools=["delete_page", "archive_page"])
        assert body.disabled_tools == ["delete_page", "archive_page"]

    def test_rejects_a_tool_name_past_the_mcp_limit(self):
        from pydantic import ValidationError

        from app.schemas.mcp_server import ConnectorUpdate

        with pytest.raises(ValidationError):
            ConnectorUpdate(disabled_tools=["x" * 129])

    def test_rejects_an_unbounded_list(self):
        from pydantic import ValidationError

        from app.schemas.mcp_server import ConnectorUpdate

        with pytest.raises(ValidationError):
            ConnectorUpdate(disabled_tools=[f"tool_{index}" for index in range(257)])
