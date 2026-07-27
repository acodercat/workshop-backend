"""OAuth 2.1 authorization-code flow for MCP Connectors.

We drive the two-legged web redirect (authorize → browser → callback) ourselves
— the SDK's ``OAuthClientProvider`` is built for a blocking local-browser client
— but reuse the SDK's low-level ``mcp.client.auth.oauth2`` helpers for metadata
discovery, dynamic client registration, and PKCE, so no OAuth crypto is
hand-written. The worker does the non-interactive connect + token refresh via
``OAuthClientProvider`` + our DB-backed ``TokenStorage``.

All failures collapse to :class:`ConnectorOAuthError` so the endpoint layer needs
no translation.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode, urlparse

import httpx
from mcp.client.auth.oauth2 import (
    PKCEParameters,
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_registration_request,
    create_oauth_metadata_request,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

from app.errors import ConnectorOAuthError, InvalidConnectorUrlError
from app.services.mcp.url_guard import guard_public_url, guarded_client

_HTTP_TIMEOUT = 20.0
_CLIENT_NAME = "Metaseq Connector"


def new_state() -> str:
    """A random, unguessable OAuth ``state`` (also the Redis key)."""
    return secrets.token_urlsafe(32)


def new_pkce() -> PKCEParameters:
    return PKCEParameters.generate()


async def discover_metadata(server_url: str) -> OAuthMetadata:
    """Discover the authorization server's OAuth metadata for an MCP server.

    The user-supplied *server_url* is pre-checked up front for a clean early
    rejection. Every actual outbound request in the OAuth flow — here and in
    :func:`ensure_client` / :func:`exchange_code` — goes through
    :func:`~app.services.mcp.url_guard.guarded_client`, whose DNS-pinning
    transport is the authoritative SSRF guard (and blocks redirects). So the
    metadata-derived endpoints (auth server, token, registration) are guarded
    without a separate check at each call site.
    """
    await guard_public_url(server_url)
    async with guarded_client(timeout=httpx.Timeout(_HTTP_TIMEOUT)) as client:
        auth_server = await _discover_auth_server(client, server_url)
        for meta_url in build_oauth_authorization_server_metadata_discovery_urls(
            auth_server, server_url
        ):
            try:
                response = await client.send(create_oauth_metadata_request(meta_url))
            except httpx.HTTPError:
                continue
            try:
                ok, metadata = handle_auth_metadata_response(response)
            except Exception:
                continue
            if ok and metadata is not None:
                return metadata
    raise ConnectorOAuthError(
        "Could not discover the MCP server's OAuth metadata."
    )


async def _discover_auth_server(client: httpx.AsyncClient, server_url: str) -> str | None:
    """Resolve the authorization server URL from protected-resource metadata."""
    for prm_url in build_protected_resource_metadata_discovery_urls(None, server_url):
        try:
            response = await client.get(prm_url)
            metadata = handle_protected_resource_response(response)
        except Exception:
            continue
        if metadata is not None and metadata.authorization_servers:
            # Attacker-controlled (it came from the remote server's metadata), but
            # the guarded_client that fetches its metadata pins DNS and rejects
            # non-public hosts, so no separate check is needed here.
            return str(metadata.authorization_servers[0])
    return None  # fall back: metadata discovery derives from server_url


async def ensure_client(
    metadata: OAuthMetadata,
    *,
    redirect_uri: str,
    scope: str | None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> OAuthClientInformationFull:
    """Return the OAuth client — the manual one if provided, else register via DCR."""
    if client_id:
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=[redirect_uri],
            scope=scope,
            token_endpoint_auth_method=(
                "client_secret_post" if client_secret else "none"
            ),
        )

    client_metadata = OAuthClientMetadata(
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
        token_endpoint_auth_method="client_secret_post",
        client_name=_CLIENT_NAME,
    )
    async with guarded_client(timeout=httpx.Timeout(_HTTP_TIMEOUT)) as client:
        try:
            request = create_client_registration_request(
                metadata, client_metadata, str(metadata.issuer)
            )
            response = await client.send(request)
            return handle_registration_response(response)
        except (ConnectorOAuthError, InvalidConnectorUrlError):
            # InvalidConnectorUrlError = guarded_client rejected the (server-
            # controlled) registration endpoint as non-public; surface it as-is.
            raise
        except Exception as exc:
            raise ConnectorOAuthError(
                f"Dynamic client registration failed: {exc}. Provide an OAuth "
                f"Client ID/Secret if this server does not support registration."
            ) from exc


def build_authorization_url(
    metadata: OAuthMetadata,
    client: OAuthClientInformationFull,
    *,
    redirect_uri: str,
    scope: str | None,
    state: str,
    pkce: PKCEParameters,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    # The endpoint may already carry a query (RFC 6749 §3.1 permits it), so pick
    # the right separator rather than a blind '?'.
    separator = "&" if urlparse(str(metadata.authorization_endpoint)).query else "?"
    return f"{metadata.authorization_endpoint}{separator}{urlencode(params)}"


async def exchange_code(
    metadata: OAuthMetadata,
    client: OAuthClientInformationFull,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> OAuthToken:
    """Exchange an authorization code for tokens at the token endpoint."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client.client_id,
        "code_verifier": code_verifier,
    }
    if client.client_secret:
        data["client_secret"] = client.client_secret

    # guarded_client pins DNS + rejects non-public hosts, so the server-supplied
    # token endpoint is SSRF-safe without a separate pre-check.
    async with guarded_client(timeout=httpx.Timeout(_HTTP_TIMEOUT)) as http_client:
        try:
            response = await http_client.post(
                str(metadata.token_endpoint),
                data=data,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ConnectorOAuthError(f"Token endpoint unreachable: {exc}") from exc

    if response.status_code != 200:
        raise ConnectorOAuthError(
            f"Token exchange failed ({response.status_code})."
        )
    try:
        return OAuthToken.model_validate(response.json())
    except Exception as exc:
        raise ConnectorOAuthError(f"Malformed token response: {exc}") from exc
