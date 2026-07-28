"""Unit tests for the Connector OAuth service (pure / no-network parts)."""

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata

from app.errors import InvalidConnectorUrlError
from app.services.mcp.oauth import (
    build_authorization_url,
    discover_metadata,
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
            redirect_uri="https://app/cb", scope=None, state="st", pkce=new_pkce(),
        )
        assert url.count("?") == 1
        assert url.startswith("https://as.example.com/authorize?tenant=acme&")
        assert "response_type=code" in url


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

    async def test_discover_metadata_rejects_internal_server_url(self):
        # Guard fires on the private IP before any network request is made.
        with pytest.raises(InvalidConnectorUrlError):
            await discover_metadata("https://10.0.0.1/mcp")

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
                code="abc", redirect_uri="https://app/cb", code_verifier="v",
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
