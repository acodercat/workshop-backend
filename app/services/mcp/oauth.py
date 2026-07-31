"""OAuth 2.1 authorization-code flow for MCP Connectors.

We drive the two-legged web redirect (authorize → browser → callback) ourselves
— the SDK's ``OAuthClientProvider`` is built for a blocking local-browser client
— but reuse the SDK's low-level ``mcp.client.auth.oauth2`` helpers for metadata
discovery, client identity (CIMD or dynamic registration), and PKCE, so no
OAuth crypto is hand-written.

**Obtaining a grant only.** Renewing one afterwards belongs to the agent worker,
which does it on demand during a turn (``infra/connector_grant_rotation``) —
because a turn can outlive its access token, and nothing on a schedule here can
close that gap. What stays is the browser flow this module exists for: authorize,
exchange, and the RFC 7009 revocation that ends a grant.

All failures collapse to :class:`ConnectorOAuthError` so the endpoint layer needs
no translation.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlparse

import httpx
from pydantic import AnyUrl
from mcp.client.auth.oauth2 import (
    PKCEParameters,
    resource_url_from_server_url,
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_registration_request,
    create_oauth_metadata_request,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
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
    ProtectedResourceMetadata,
)
from mcp.shared.auth_utils import check_resource_allowed

from app.errors import ConnectorOAuthError, InvalidConnectorUrlError
from app.services.mcp.url_guard import guard_public_url, guarded_client

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 20.0
# Ceiling on a whole discovery, not on each of its requests. ``httpx.Timeout``
# bounds a read; a server answering a byte every nineteen seconds resets that
# clock forever, and this runs on an interactive endpoint holding a request and
# a database session. One discovery is at most six requests (the 401 challenge,
# two protected-resource candidates, three authorization-server ones), so a
# per-call bound would let a working-but-slow server take two minutes.
_DISCOVERY_TIMEOUT = 30.0
_CLIENT_NAME = "Metaseq Connector"
# The scope that asks for a refresh token (SEP-2207). Requested whenever the
# authorization server advertises it; without one, every access-token expiry
# becomes a browser round-trip.
_OFFLINE_ACCESS = "offline_access"


def new_state() -> str:
    """A random, unguessable OAuth ``state`` (also the Redis key)."""
    return secrets.token_urlsafe(32)


def new_pkce() -> PKCEParameters:
    return PKCEParameters.generate()


def require_matching_issuer(
    returned: str | None, *, recorded: str | None, advertised: bool,
) -> None:
    """Check who issued an authorization response, before its code is spent.

    RFC 9207 §2.4, which MCP makes a client MUST. It closes the *mix-up*
    attack, whose precondition is exactly this service's shape: one client, one
    redirect URI, many unrelated authorization servers.

    The attack: a Connector the user was talked into adding points at the
    attacker's authorization server, so the flow under ``state`` S is recorded
    as theirs. The attacker replays S — and the ``code_challenge`` they saw in
    the URL we opened for them — into an authorization request at the user's
    *real* provider. The real provider redirects a real code to this callback
    under S, and without this check we would post that code, and the PKCE
    verifier with it, to the attacker's token endpoint. PKCE does not help:
    it guards a code intercepted *without* the verifier, and here we hand the
    verifier over ourselves.

    Four cases, per the RFC's table:

    ============================  ==============  ========================
    ``iss`` advertised            ``iss`` present  action
    ============================  ==============  ========================
    yes                           yes              compare
    yes                           no               reject
    no                            yes              compare anyway
    no                            no               proceed
    ============================  ==============  ========================

    The third row is the RFC's local-policy provision, which MCP takes: servers
    that emit ``iss`` before advertising it still get checked.

    Compared byte for byte. The RFC forbids case folding, default-port elision,
    trailing-slash and percent-encoding normalisation, so *recorded* has to be
    the string the metadata document published — see
    :attr:`DiscoveredAuthorization.issuer_as_published`.

    A ``recorded`` of ``None`` only happens for a pending authorization started
    before this check existed; it fails closed, costing that user one more
    click on Connect within the state's ten-minute life.
    """
    if advertised and returned is None:
        raise ConnectorOAuthError(
            "authorization server advertises the iss parameter but omitted it."
        )
    if returned is None:
        return
    if returned != recorded:
        raise ConnectorOAuthError(
            "authorization response came from a different authorization server "
            "than the one this flow was started with."
        )


def _split(scope: str | None) -> list[str]:
    """A space-delimited ``scope`` value as its individual scopes."""
    return scope.split() if scope else []


def _deduplicate(scopes: list[str]) -> list[str]:
    """*scopes* without repeats, first occurrence winning.

    Order is kept because the challenge comes first, which puts what the server
    actually asked for at the front of what the user is shown consenting to.
    """
    return list(dict.fromkeys(scopes))


@dataclass(frozen=True)
class DiscoveredAuthorization:
    """How to authorize against one MCP server, as its metadata declares it."""

    metadata: OAuthMetadata
    #: The server's RFC 9728 document. Required, not merely usual: it is the
    #: only thing that names the authorization server.
    resource: ProtectedResourceMetadata
    #: ``scope`` from the server's 401 challenge, when it issued one.
    challenge_scope: str | None = None
    #: ``issuer`` exactly as the document published it. RFC 9207 forbids
    #: normalising either side before comparison, and :attr:`metadata`'s went
    #: through pydantic's ``AnyUrl``, which canonicalises — a bare origin gains
    #: a trailing slash. Comparing against that would reject every provider
    #: whose issuer is a bare origin.
    issuer_as_published: str | None = None
    #: Whether the server advertises RFC 9207 ``iss`` on authorization
    #: responses. Not modelled by the SDK, so it is read from the document.
    #: Both fields go when the SDK reaches 2.x, which models this one and stops
    #: canonicalising the other.
    iss_parameter_supported: bool = False

    @property
    def scopes_supported(self) -> list[str] | None:
        """What the resource says it can grant, if it said."""
        return self.resource.scopes_supported

    def as_stored(self) -> dict:
        """Both documents, in the shape ``mcp_server.oauth_metadata`` holds.

        Kept so that neither a refresh nor a revocation has to rediscover
        anything: the endpoint to post to, and the resource an indicator must
        name. Read back by :func:`app.services.mcp.grant.stored_authorization`,
        which every caller goes through — rename a key here and it silently
        reads ``None`` there.
        """
        return {
            "authorization_server": self.metadata.model_dump(mode="json"),
            "protected_resource": self.resource.model_dump(mode="json"),
        }

    def scope_for(self, configured: str | None) -> str | None:
        """The scope to request on the authorization leg.

        The 401 challenge is what the server says *this* request needs, and MCP
        requires clients to treat it as authoritative — so it is never dropped,
        only added to. An operator's configured scope is a request for more
        than the minimum, so the two are unioned rather than ranked: replacing
        the challenge with a configuration would authorize successfully and
        still leave the token unable to touch the resource.

        With no challenge, a configured scope *replaces* the advertised set
        rather than joining it. The spec reaches for ``scopes_supported``
        precisely because "MCP clients typically lack domain-specific knowledge
        to make informed decisions about individual scope selection" — an
        operator who named the scopes is that knowledge, and unioning would
        turn a deliberate ``repo:read`` into the whole advertised set,
        ``admin:org`` included. With neither, the advertised set is the
        fallback; with none of the three, ``scope`` is omitted entirely.

        ``offline_access`` is then added whenever the server advertises it
        (SEP-2207). It asks for a refresh token rather than for access to
        anything, so neither a challenge — which names resource scopes and
        nothing else — nor an operator naming the data they want has thereby
        opted out of silent renewal. Dropping it leaves a Connector demanding a
        browser round-trip every time its access token ages out.
        """
        if self.challenge_scope:
            # Authoritative, so it is joined rather than chosen between.
            requested = _split(self.challenge_scope) + _split(configured)
        else:
            requested = _split(configured) or list(self.scopes_supported or [])
        return self._with_offline_access(" ".join(_deduplicate(requested)) or None)

    def _with_offline_access(self, requested: str | None) -> str | None:
        """*requested* plus ``offline_access``, when the server offers it.

        Read from the **authorization server's** metadata, which is where
        SEP-2207 puts it. The resource document is the wrong place to look:
        ``offline_access`` is a matter between client and authorization server,
        never a requirement of the resource, so a conformant MCP server is told
        not to advertise it — checking there would make this dead code against
        exactly the servers that follow the spec.

        Only when advertised: asking for a scope the server never listed can
        have the whole authorization request rejected.
        """
        advertised = self.metadata.scopes_supported or []
        if _OFFLINE_ACCESS not in advertised:
            return requested
        scopes = requested.split() if requested else []
        if _OFFLINE_ACCESS in scopes:
            return requested
        return " ".join([*scopes, _OFFLINE_ACCESS])


async def discover_authorization(server_url: str) -> DiscoveredAuthorization:
    """Discover an MCP server's authorization server metadata and declared scopes.

    The user-supplied *server_url* is pre-checked up front for a clean early
    rejection. Every actual outbound request in the OAuth flow — here and in
    :func:`ensure_client` / :func:`exchange_code` — goes through
    :func:`~app.services.mcp.url_guard.guarded_client`, whose DNS-pinning
    transport is the authoritative SSRF guard (and blocks redirects). So the
    metadata-derived endpoints (auth server, token, registration) are guarded
    without a separate check at each call site.

    Bounded as a whole rather than per request: a server that answers slowly
    enough never trips ``httpx``'s per-read timeout, and this runs while a
    request and its database session are held open.

    """
    try:
        async with asyncio.timeout(_DISCOVERY_TIMEOUT):
            # Inside the deadline, not before it: the guard resolves DNS, and a
            # blackholed name has no bound of its own to stop at.
            await guard_public_url(server_url)
            return await _discover(server_url)
    except TimeoutError as exc:
        raise ConnectorOAuthError(
            "The MCP server's authorization metadata took too long to discover."
        ) from exc


async def _discover(server_url: str) -> DiscoveredAuthorization:
    """The discovery sequence itself, run under its caller's deadline."""
    async with guarded_client(timeout=httpx.Timeout(_HTTP_TIMEOUT)) as client:
        challenge = await _read_challenge(client, server_url)
        resource = await _discover_protected_resource(
            client, server_url, challenge.resource_metadata_url,
        )
        if resource is None:
            raise ConnectorOAuthError(
                "This MCP server publishes no OAuth 2.0 Protected Resource "
                "Metadata (RFC 9728), so there is no way to tell which "
                "authorization server it trusts. Ask its operator to publish "
                "one, or connect it with an API key instead."
            )
        metadata, document = await _discover_authorization_server(
            client, server_url, resource,
        )

    _require_https_endpoints(metadata)
    _require_pkce_support(metadata)
    return DiscoveredAuthorization(
        metadata=metadata,
        resource=resource,
        challenge_scope=challenge.scope,
        issuer_as_published=document.get("issuer"),
        iss_parameter_supported=bool(
            document.get("authorization_response_iss_parameter_supported")
        ),
    )


async def _discover_authorization_server(
    client: httpx.AsyncClient,
    server_url: str,
    resource: ProtectedResourceMetadata,
) -> tuple[OAuthMetadata, dict]:
    """The authorization server's own metadata, from the first URL that has it.

    Returned with the document it was parsed from: the parsed model
    canonicalises URLs and drops fields it does not model, and both matter for
    :func:`require_matching_issuer`.

    Candidates come from the authorization server the resource document names,
    and only from there. There is no falling back to the MCP origin when a
    server publishes no resource document: the spec's discovery sequence ends
    that branch at "abort or use pre-configured values", and guessing that a
    resource server is also its own authorization server is exactly the guess
    RFC 9728 exists to remove. The SDK keeps such a fallback for servers built
    against an older spec, which is why the candidate builder still accepts a
    ``None`` first argument — we never pass one, so *server_url* reaches it only
    to satisfy that signature and no candidate is ever built from it.

    Raises rather than returning ``None``: without this document there is no
    endpoint to send anyone to.
    """
    authorization_server = str(resource.authorization_servers[0])
    # Whose document we are asking for, and therefore whose ``issuer`` the
    # answer must claim (RFC 8414 §3.3). Put through ``AnyUrl`` because the
    # compared ``issuer`` comes back through it too and it canonicalises (a
    # bare origin gains a trailing slash, a path does not). Matching
    # serialisations is what lets the comparison itself stay exact.
    expected_issuer = str(AnyUrl(authorization_server))
    for metadata_url in build_oauth_authorization_server_metadata_discovery_urls(
        authorization_server, server_url
    ):
        try:
            response = await client.send(create_oauth_metadata_request(metadata_url))
        except httpx.HTTPError:
            continue
        try:
            keep_trying, metadata = await handle_auth_metadata_response(response)
        except Exception:
            continue
        if metadata is not None and not _issued_by(metadata, expected_issuer):
            continue
        if metadata is not None:
            return metadata, response.json()
        if not keep_trying and response.status_code >= 500:
            # The SDK reads *any* non-4XX as "the remaining candidates are no
            # more likely to answer than this one". True of a server error;
            # not of a redirect, which says only that this one URL moved. The
            # SDK never has to tell them apart because its client follows
            # redirects — ours refuses to, so that it cannot be bounced onto
            # an internal host, and a provider whose legacy path 301s to its
            # canonical one would otherwise take the OIDC candidate behind it
            # down with it.
            break
    raise ConnectorOAuthError(
        "Could not discover the MCP server's OAuth metadata."
    )


@dataclass(frozen=True)
class _Challenge:
    """What the MCP server's 401 ``WWW-Authenticate`` said, if it issued one."""

    resource_metadata_url: str | None = None
    scope: str | None = None


async def _read_challenge(client: httpx.AsyncClient, server_url: str) -> _Challenge:
    """Ask the MCP server unauthenticated and read its 401 challenge.

    MCP requires clients to support both discovery routes, and this is the one
    only the server can answer: a conforming server may publish its resource
    metadata at a URL no ``.well-known`` guess would reach, and its challenge
    names the scopes *this* resource needs rather than everything the
    authorization server might ever grant.

    A bare ``GET`` is the SSE-open shape. A server that takes it as an
    invitation rather than refusing it answers with a stream instead of a 401 —
    and httpx bounds each *read*, not the request, so its heartbeats would reset
    that clock indefinitely and hang the authorize endpoint on a request slot
    and a database session. Hence a deadline over the whole call.

    Best-effort by design: a server that answers anything but a 401 with a
    usable header simply contributes nothing, and the ``.well-known`` route
    carries the discovery on its own.
    """
    try:
        async with asyncio.timeout(_HTTP_TIMEOUT):
            response = await client.get(
                server_url, headers={"Accept": "text/event-stream"}
            )
    except (httpx.HTTPError, TimeoutError):
        return _Challenge()
    if response.status_code != 401:
        return _Challenge()
    return _Challenge(
        resource_metadata_url=extract_resource_metadata_from_www_auth(response),
        scope=extract_scope_from_www_auth(response),
    )


def _issued_by(metadata: OAuthMetadata, expected: str) -> bool:
    """Whether a metadata document claims the issuer whose URL it came from.

    RFC 8414 §3.3: the ``issuer`` in the response MUST match the one the
    well-known URL was built from, and data that fails MUST NOT be used. The
    SDK's handler only parses, so an authorization server answering for a
    *different* tenant than the resource document named would otherwise be
    accepted — and the browser, the authorization code, and the exchange would
    all go to that tenant instead.
    """
    # Compared exactly, not normalised: RFC 8414 §3.3 says the values must be
    # identical, and ``/tenant`` and ``/tenant/`` are two tenants as far as an
    # authorization server is concerned. Trimming either would merge them.
    if str(metadata.issuer) == expected:
        return True
    logger.warning(
        "Ignoring authorization server metadata from %s: it claims issuer %s",
        expected, metadata.issuer,
    )
    return False


def _describes(resource: ProtectedResourceMetadata, server_url: str) -> bool:
    """Whether a resource document actually describes *server_url*.

    The SDK's own ``_validate_resource_match``, which driving the flow by hand
    skips. Without it a server can publish a document naming someone else's
    resource and pointing at *their* authorization server: the browser would be
    sent to a login page the user rightly trusts, to authorize a grant we then
    hold against a different host entirely. RFC 9728 makes ``resource``
    mandatory, so there is no absent case to wave through.
    """
    if check_resource_allowed(
        requested_resource=resource_url_from_server_url(server_url),
        configured_resource=str(resource.resource),
    ):
        return True
    logger.warning(
        "Ignoring an MCP server's resource metadata: it claims %s, which does "
        "not cover %s", resource.resource, server_url,
    )
    return False


def _require_https_endpoints(metadata: OAuthMetadata) -> None:
    """Refuse an authorization server that publishes a plaintext endpoint.

    ``guarded_client`` already enforces https on everything *we* fetch, but the
    authorization endpoint is the one the **browser** is sent to — no transport
    of ours is in that path, and a plaintext one would carry the client id,
    state and PKCE challenge in the clear. Checked at discovery so a bad
    document is rejected before any of it is stored.
    """
    endpoints = {
        "authorization_endpoint": metadata.authorization_endpoint,
        "token_endpoint": metadata.token_endpoint,
        "registration_endpoint": metadata.registration_endpoint,
    }
    for name, endpoint in endpoints.items():
        if endpoint is None:
            continue
        if urlparse(str(endpoint)).scheme != "https":
            raise ConnectorOAuthError(
                f"The authorization server's {name} is not https, "
                f"which MCP requires."
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


async def _discover_protected_resource(
    client: httpx.AsyncClient, server_url: str, challenge_url: str | None,
):
    """The server's RFC 9728 metadata — authorization servers *and* scopes.

    Both come from the one document, so it is fetched once and returned whole
    rather than pulled apart here. ``None`` when undiscoverable, which the
    caller turns into a refusal — there is nothing left to discover from.

    *challenge_url* is the ``resource_metadata`` the server named in its 401,
    which the SDK puts first among the candidates — a conforming server may
    publish the document nowhere else.
    """
    for resource_metadata_url in build_protected_resource_metadata_discovery_urls(
        challenge_url, server_url
    ):
        try:
            response = await client.get(resource_metadata_url)
            metadata = await handle_protected_resource_response(response)
        except Exception:
            continue
        if (
            metadata is not None
            and metadata.authorization_servers
            and _describes(metadata, server_url)
        ):
            # Its authorization server is attacker-controlled input, but the
            # guarded_client that fetches metadata from it pins DNS and rejects
            # non-public hosts, so SSRF needs no separate check here. What
            # ``_describes`` adds is that the document is talking about *us*.
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
            token_endpoint_auth_method=_manual_auth_method(metadata, client_secret),
        )

    if should_use_client_metadata_url(metadata, client_metadata_url):
        # Checked here, not up front: a malformed URL must only fail servers
        # that would actually have used it, never the DCR path.
        if not is_valid_client_metadata_url(client_metadata_url):
            raise ConnectorOAuthError(
                "mcp_client_metadata_url must be an https URL with a non-root path."
            )
        return cimd_client(client_metadata_url, redirect_uri=redirect_uri)

    return await _register_client(metadata, redirect_uri=redirect_uri, scope=scope)


async def _register_client(
    metadata: OAuthMetadata, *, redirect_uri: str, scope: str | None,
) -> OAuthClientInformationFull:
    """Register a client with the server (RFC 7591 dynamic registration).

    The last resort: no client was configured and the server does not advertise
    CIMD, so it has to mint us one.

    Only if it says it can. MCP ranks DCR third *and* conditions it on
    ``registration_endpoint`` being advertised, and the SDK's request builder
    does not check — handed metadata without one it falls back to guessing
    ``{issuer}/register``, which for a provider that simply does not do DCR is
    an ordinary web route on their site. GitHub's remote MCP server is the case
    in point: it requires a pre-registered OAuth app, so the guess POSTs a
    registration document at ``https://github.com/register``, collects a 422,
    and the user is told their Connector could not be added rather than what to
    do about it.
    """
    if metadata.registration_endpoint is None:
        raise ConnectorOAuthError(
            "This server's authorization server does not support dynamic "
            "client registration, so it cannot issue us a client. Register an "
            "OAuth app with the provider and add this Connector with its "
            "Client ID and Secret."
        )
    client_metadata = OAuthClientMetadata(
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
        # Ask for what the server says it accepts: a registration endpoint that
        # only does Basic rejects the request outright rather than substituting
        # a method, so a hardcoded preference locks us out of those servers.
        token_endpoint_auth_method=_preferred_auth_method(metadata),
        client_name=_CLIENT_NAME,
    )
    # The deadline sits outside the ``try`` so its ``TimeoutError`` lands in the
    # same conversion as every other failure — inside, it would escape as a raw
    # 500 rather than the 502 a failed registration reads as.
    try:
        async with asyncio.timeout(_HTTP_TIMEOUT), guarded_client(
            timeout=httpx.Timeout(_HTTP_TIMEOUT)
        ) as client:
            request = create_client_registration_request(
                metadata, client_metadata, str(metadata.issuer)
            )
            response = await client.send(request)
            # Kept whole: the server may substitute a different
            # ``token_endpoint_auth_method`` than the one asked for
            # (RFC 7591 §3.2.1), and that answer is what we authenticate with.
            return _pin_auth_method(await handle_registration_response(response))
    except (ConnectorOAuthError, InvalidConnectorUrlError):
        # InvalidConnectorUrlError = guarded_client rejected the (server-
        # controlled) registration endpoint as non-public; surface it as-is.
        raise
    except Exception as exc:
        raise ConnectorOAuthError(
            f"Dynamic client registration failed: {exc}. Provide an OAuth "
            f"Client ID/Secret if this server does not support registration."
        ) from exc


def _pin_auth_method(
    client: OAuthClientInformationFull,
) -> OAuthClientInformationFull:
    """Resolve an omitted ``token_endpoint_auth_method`` before the row is stored.

    The one place the RFC 7591 §2 default is applied, because the client is read
    back twice — by the code exchange here and, later, by every renewal the
    worker makes — and an absent method reads as "send no secret at all".
    Deciding it once, on the bytes they share, is what stops an authorization
    that worked from becoming a renewal that doesn't.

    Only a registered client can arrive unresolved; the manual and CIMD paths
    both state a method outright.
    """
    if client.token_endpoint_auth_method is not None:
        return client
    return client.model_copy(
        update={"token_endpoint_auth_method": "client_secret_basic"}
    )


def _preferred_auth_method(metadata: OAuthMetadata) -> str:
    """The client-authentication method to use with *metadata*'s server.

    The first of ours the server declares, most preferred first. ``none`` is
    among them because a public-only authorization server rejects a
    registration that asks to authenticate at all — and its own declaration is
    the only evidence for that.

    Declaring nothing falls back to Basic, the RFC 7591 §2 default. So does
    declaring only methods we cannot perform (``private_key_jwt``): the request
    is refused either way, and refused with a method we support gives the
    clearer error.
    """
    supported = metadata.token_endpoint_auth_methods_supported or []
    for method in ("client_secret_post", "client_secret_basic", "none"):
        if method in supported:
            return method
    return "client_secret_basic"


def _manual_auth_method(metadata: OAuthMetadata, client_secret: str | None) -> str:
    """How a hand-configured client authenticates at the token endpoint.

    Nothing registered this client with us, so there is no echoed method to
    honour and the server's own declaration is the only evidence.
    """
    if not client_secret:
        return "none"
    return _preferred_auth_method(metadata)


def _apply_client_auth(
    client: OAuthClientInformationFull,
    data: dict[str, str],
    headers: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Authenticate a token request the way the client is registered to.

    A port of the SDK's ``OAuthContext.prepare_token_auth`` rather than a call
    to it: that is a method on a dataclass assembled around a ``TokenStorage``
    nothing here has. Like the SDK's, it takes the method at face value —
    resolving an absent one is :func:`_pin_auth_method`'s job, at the point the
    client is minted, so the code exchange and every later rotation
    authenticate identically.
    """
    method = client.token_endpoint_auth_method
    if method == "client_secret_basic" and client.client_id and client.client_secret:
        # Percent-encoded before base64 per RFC 6749 §2.3.1 — a secret
        # containing ':' would otherwise split into the wrong credentials.
        credentials = (
            f"{quote(client.client_id, safe='')}:"
            f"{quote(client.client_secret, safe='')}"
        )
        encoded = base64.b64encode(credentials.encode()).decode()
        headers = {**headers, "Authorization": f"Basic {encoded}"}
    elif method == "client_secret_post" and client.client_secret:
        data = {**data, "client_secret": client.client_secret}
    return data, headers


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
    data, headers = _apply_client_auth(
        client, data, {"Accept": "application/json"}
    )

    response = await _post(str(metadata.token_endpoint), data, headers)
    if response.status_code != 200:
        raise ConnectorOAuthError(
            f"Token exchange failed ({response.status_code})."
        )
    try:
        return OAuthToken.model_validate(response.json())
    except Exception as exc:
        raise ConnectorOAuthError(f"Malformed token response: {exc}") from exc


async def revoke_grant(
    metadata: OAuthMetadata,
    client: OAuthClientInformationFull,
    *,
    tokens: OAuthToken,
) -> None:
    """Tell the authorization server we are finished with a grant (RFC 7009).

    Sent when the user disconnects or deletes a Connector. Dropping our copy of
    the tokens is enough to stop *us* using them, but it is not what the user
    was promised: without this the authorization stays live at the provider, and
    they find an application they disconnected still listed on their account
    page, still holding a refresh token that outlives every trace of it here.

    The refresh token is what gets sent when there is one. RFC 7009 §2.1 asks a
    server that revokes one to invalidate the whole grant family behind it, so
    that single request ends the access tokens too; revoking an access token
    only ends that token, and the refresh token would mint another. Falling back
    to the access token covers the providers that issue no refresh token at all.

    Authenticated exactly as a token request is. A separate
    ``revocation_endpoint_auth_methods_supported`` exists, but RFC 7009 §2.1
    says the client authenticates as it does at the token endpoint, and a client
    registered for one method is registered for both.

    Only called when *metadata* advertises the endpoint — a server that does not
    is a server with nothing to tell. Failure raises like every sibling here;
    the caller decides what a failure is worth, and for the one caller today the
    answer is a log line.
    """
    if metadata.revocation_endpoint is None:
        raise ConnectorOAuthError(
            "This authorization server advertises no revocation endpoint."
        )
    data = {
        "token": tokens.refresh_token or tokens.access_token,
        "token_type_hint": (
            "refresh_token" if tokens.refresh_token else "access_token"
        ),
        "client_id": client.client_id,
    }
    data, headers = _apply_client_auth(
        client, data, {"Accept": "application/json"}
    )

    response = await _post(str(metadata.revocation_endpoint), data, headers)
    # 200 is the whole of success, and it is also what a server answers for a
    # token it does not recognise (RFC 7009 §2.2) — already revoked, already
    # expired, never valid. Nothing here can tell those apart, and nothing here
    # needs to: they are all "the provider is not holding this".
    if response.status_code != 200:
        raise ConnectorOAuthError(f"Revocation failed ({response.status_code}).")


async def _post(
    url: str, data: dict[str, str], headers: dict[str, str],
) -> httpx.Response:
    """POST to one of the provider's endpoints, under a deadline over the call.

    ``httpx.Timeout`` bounds each *read*, not the request — the same trap
    :func:`_read_challenge` documents. An endpoint that answers a byte every
    nineteen seconds keeps resetting that clock, and these calls are made inside
    a request the user is waiting on.

    ``guarded_client`` pins DNS and rejects non-public hosts, so the
    server-supplied endpoint is SSRF-safe without a separate pre-check.
    """
    try:
        async with asyncio.timeout(_HTTP_TIMEOUT):
            async with guarded_client(
                timeout=httpx.Timeout(_HTTP_TIMEOUT)
            ) as http_client:
                return await http_client.post(url, data=data, headers=headers)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise ConnectorOAuthError(f"OAuth endpoint unreachable: {exc}") from exc
