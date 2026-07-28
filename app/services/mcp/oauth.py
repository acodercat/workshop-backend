"""OAuth 2.1 authorization-code flow for MCP Connectors.

We drive the two-legged web redirect (authorize → browser → callback) ourselves
— the SDK's ``OAuthClientProvider`` is built for a blocking local-browser client
— but reuse the SDK's low-level ``mcp.client.auth.oauth2`` helpers for metadata
discovery, client identity (CIMD or dynamic registration), and PKCE, so no
OAuth crypto is hand-written. The worker does the non-interactive connect +
token refresh via ``OAuthClientProvider`` + our DB-backed ``TokenStorage``.

All failures collapse to :class:`ConnectorOAuthError` so the endpoint layer needs
no translation.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx
from mcp.client.auth.oauth2 import (
    PKCEParameters,
    resource_url_from_server_url,
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_registration_request,
    create_oauth_metadata_request,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
    is_valid_client_metadata_url,
    should_use_client_metadata_url,
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


@dataclass(frozen=True)
class DiscoveredAuthorization:
    """How to authorize against one MCP server, as its metadata declares it."""

    metadata: OAuthMetadata
    scopes_supported: list[str] | None = None

    def scope_for(self, configured: str | None) -> str | None:
        """The scope to request on the authorization leg.

        MCP's scope-selection strategy: a general-purpose client has no
        domain knowledge to pick scopes with, so it asks for what the server
        advertises and lets the consent screen decide. An explicitly
        configured scope still wins — that is an operator's choice, not a
        guess. Neither present means omitting ``scope`` entirely.
        """
        if configured:
            return configured
        return " ".join(self.scopes_supported) if self.scopes_supported else None


async def discover_authorization(server_url: str) -> DiscoveredAuthorization:
    """Discover an MCP server's authorization server metadata and declared scopes.

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
        resource = await _discover_protected_resource(client, server_url)
        authorization_server = (
            str(resource.authorization_servers[0])
            if resource is not None and resource.authorization_servers
            else None
        )
        for metadata_url in build_oauth_authorization_server_metadata_discovery_urls(
            authorization_server, server_url
        ):
            try:
                response = await client.send(create_oauth_metadata_request(metadata_url))
            except httpx.HTTPError:
                continue
            try:
                ok, metadata = handle_auth_metadata_response(response)
            except Exception:
                continue
            if ok and metadata is not None:
                _require_pkce_support(metadata)
                return DiscoveredAuthorization(
                    metadata=metadata,
                    scopes_supported=(
                        resource.scopes_supported if resource is not None else None
                    ),
                )
    raise ConnectorOAuthError(
        "Could not discover the MCP server's OAuth metadata."
    )


def _require_pkce_support(metadata: OAuthMetadata) -> None:
    """Refuse an authorization server that does not advertise PKCE.

    The metadata is the only signal MCP offers, and falling back to a bare
    authorization code would leave it interceptable.
    """
    methods = metadata.code_challenge_methods_supported
    if not methods or "S256" not in methods:
        raise ConnectorOAuthError(
            "The authorization server does not advertise PKCE support "
            "(code_challenge_methods_supported with S256), which MCP requires."
        )


async def _discover_protected_resource(client: httpx.AsyncClient, server_url: str):
    """The server's RFC 9728 metadata — authorization servers *and* scopes.

    Both come from the one document, so it is fetched once and returned whole
    rather than pulled apart here. ``None`` when undiscoverable: authorization
    server discovery then derives from *server_url*, and scope selection falls
    back to whatever the caller configured.
    """
    for resource_metadata_url in build_protected_resource_metadata_discovery_urls(
        None, server_url
    ):
        try:
            response = await client.get(resource_metadata_url)
            metadata = handle_protected_resource_response(response)
        except Exception:
            continue
        if metadata is not None and metadata.authorization_servers:
            # Attacker-controlled (it came from the remote server's metadata), but
            # the guarded_client that fetches its metadata pins DNS and rejects
            # non-public hosts, so no separate check is needed here.
            return metadata
    return None


def cimd_client(
    client_metadata_url: str, *, redirect_uri: str
) -> OAuthClientInformationFull:
    """Our OAuth identity when the client id is a published URL (CIMD).

    ``client_id`` echoes the document's own URL — that binding is what stops the
    document being republished elsewhere under a different identity. Public
    client: the id is world-readable, so there is no secret to authenticate
    with. No ``scope`` — it varies per Connector and rides on the authorization
    request. ``grant_types`` / ``response_types`` come from the model defaults.
    """
    return OAuthClientInformationFull(
        client_id=client_metadata_url,
        client_name=_CLIENT_NAME,
        redirect_uris=[redirect_uri],
        token_endpoint_auth_method="none",
    )


def client_metadata_document(client_metadata_url: str, *, redirect_uri: str) -> dict:
    """The published form of :func:`cimd_client` — what the server fetches.

    Serialized from the very object we authorize with, so the identity we
    publish and the identity we use cannot drift.
    """
    return cimd_client(client_metadata_url, redirect_uri=redirect_uri).model_dump(
        mode="json", exclude_none=True
    )


async def ensure_client(
    metadata: OAuthMetadata,
    *,
    redirect_uri: str,
    scope: str | None,
    client_id: str | None = None,
    client_secret: str | None = None,
    client_metadata_url: str | None = None,
) -> OAuthClientInformationFull:
    """Return the OAuth client: a manually-configured one, else CIMD, else DCR.

    CIMD is preferred over registration when the server advertises it (MCP
    ranks it above DCR): the client id is our published metadata URL, so there
    is no registration round-trip and nothing per-Connector to store.
    """
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

    if should_use_client_metadata_url(metadata, client_metadata_url):
        # Checked here, not up front: a malformed URL must only fail servers
        # that would actually have used it, never the DCR path.
        if not is_valid_client_metadata_url(client_metadata_url):
            raise ConnectorOAuthError(
                "mcp_client_metadata_url must be an https URL with a non-root path."
            )
        return cimd_client(client_metadata_url, redirect_uri=redirect_uri)

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
    server_url: str,
    redirect_uri: str,
    scope: str | None,
    state: str,
    pkce: PKCEParameters,
) -> str:
    """The provider URL to send the browser to.

    Carries the RFC 8707 ``resource`` indicator so the issued token is bound to
    this MCP server rather than replayable against another.
    """
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
        "resource": resource_url_from_server_url(server_url),
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
    server_url: str,
    redirect_uri: str,
    code_verifier: str,
) -> OAuthToken:
    """Exchange an authorization code for tokens at the token endpoint.

    The ``resource`` indicator must match the authorization leg's (RFC 8707).
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client.client_id,
        "code_verifier": code_verifier,
        "resource": resource_url_from_server_url(server_url),
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
