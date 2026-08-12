"""Unit tests for the Connector OAuth service (pure / no-network parts)."""

import asyncio
import base64
import inspect
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    ProtectedResourceMetadata,
)

from app.errors import (
    ConnectorAuthorizationExpiredError,
    ConnectorAuthorizationSupersededError,
    ConnectorChangedDuringAuthorizationError,
    ConnectorCredentialUnreadableError,
    ConnectorNotAuthorizedError,
    ConnectorOAuthChangeUnsupportedError,
    ConnectorOAuthClientUnusableError,
    ConnectorOAuthError,
    InvalidConnectorUrlError,
    OAuthNotConfiguredError,
)
from app.services.mcp import oauth as oauth_service
from app.services.mcp.oauth import (
    DiscoveredAuthorization,
    _apply_client_auth,
    _read_challenge,
    _require_pkce_support,
    build_authorization_url,
    cimd_client,
    client_metadata_document,
    discover_authorization,
    ensure_client,
    exchange_code,
    new_pkce,
    new_state,
    require_matching_issuer,
    revoke_grant,
)

# Stands in for ``settings.mcp_client_name``, which is a per-deployment brand and
# so is never a literal in this module's code either.
CLIENT_NAME = "Test Connector"


def _metadata() -> OAuthMetadata:
    return OAuthMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        response_types_supported=["code"],
    )


class _Unread(httpx.SyncByteStream, httpx.AsyncByteStream):
    """*data*, as a stream nobody has consumed yet.

    ``httpx.Response(content=...)`` hands its bytes over eagerly and marks the
    stream **consumed**, so reading such a response with ``aiter_raw`` — which
    the bounded read does, to count wire bytes before anything decodes them —
    raises. A response off a real socket arrives unconsumed, and reading one
    without buffering it whole is the property under test.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __iter__(self):
        yield self._data

    async def __aiter__(self):
        yield self._data


def _streaming(handler):
    """*handler*, with each answer rebuilt around an unconsumed stream.

    Wraps the one handler rather than the thirty-odd ``httpx.Response(...)``
    calls behind it, so a test writes the response it means and this is where
    the double is made faithful.

    Always async, and awaits the wrapped handler when it is: ``MockTransport``
    accepts either, and the tests use both — the ones that stall a reply need a
    coroutine to sleep in.
    """

    async def rebuilt(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        if inspect.isawaitable(response):
            response = await response
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() != "content-length"
        }
        return httpx.Response(
            response.status_code,
            headers=headers,
            stream=_Unread(response.content),
        )

    return rebuilt


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id="cid", redirect_uris=["https://app/cb"])


class TestBuildAuthorizationUrl:
    def test_includes_pkce_and_state(self):
        url = build_authorization_url(
            _metadata(),
            _client(),
            server_url="https://mcp.example.com/mcp",
            redirect_uri="https://app/cb",
            scope="read",
            state="st",
            pkce=new_pkce(),
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
            _metadata(),
            _client(),
            server_url="https://mcp.example.com/mcp",
            redirect_uri="https://app/cb",
            scope=None,
            state="st",
            pkce=new_pkce(),
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
            metadata,
            _client(),
            server_url="https://mcp.example.com/mcp",
            redirect_uri="https://app/cb",
            scope=None,
            state="st",
            pkce=new_pkce(),
        )
        assert url.count("?") == 1
        assert url.startswith("https://as.example.com/authorize?tenant=acme&")
        assert "response_type=code" in url


class TestSpecRequirements:
    """The two MCP MUSTs the browser-redirect legs must satisfy themselves.
    Driving the flow by hand means the SDK's provider is not there to."""

    def test_authorization_carries_the_resource_indicator(self):
        # RFC 8707: binds the token to this server so it can't be replayed.
        url = build_authorization_url(
            _metadata(),
            _client(),
            server_url="https://mcp.example.com/mcp",
            redirect_uri="https://app/cb",
            scope=None,
            state="st",
            pkce=new_pkce(),
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
    """A hand-configured client. Nothing registered it with us, so the server's
    own declaration is the only evidence for how it should authenticate."""

    async def _client(self, supported, *, client_secret="sek"):
        return await ensure_client(
            OAuthMetadata(
                issuer="https://as.example.com",
                authorization_endpoint="https://as.example.com/authorize",
                token_endpoint="https://as.example.com/token",
                response_types_supported=["code"],
                token_endpoint_auth_methods_supported=supported,
            ),
            redirect_uri="https://app/cb",
            scope="read",
            client_name=CLIENT_NAME,
            client_id="cid",
            client_secret=client_secret,
        )

    async def test_manual_confidential_client_no_network(self):
        client = await self._client(["client_secret_post"])
        assert client.client_id == "cid"
        assert client.client_secret == "sek"
        assert client.token_endpoint_auth_method == "client_secret_post"

    async def test_manual_public_client_when_no_secret(self):
        client = await self._client(["client_secret_post"], client_secret=None)
        assert client.token_endpoint_auth_method == "none"

    async def test_a_public_only_server_is_not_asked_to_authenticate(self):
        # An authorization server declaring only ``none`` rejects a client that
        # asks to authenticate at all; its declaration is the only evidence.
        client = await self._client(["none"])
        assert client.token_endpoint_auth_method == "none"

    async def test_uses_basic_when_that_is_all_the_server_takes(self):
        # The exchange that could never complete before: the secret went in the
        # body to a server that only reads the Authorization header.
        client = await self._client(["client_secret_basic"])
        assert client.token_endpoint_auth_method == "client_secret_basic"

    async def test_defaults_to_basic_when_the_server_declares_nothing(self):
        # RFC 8414 gives token_endpoint_auth_methods_supported that same default.
        client = await self._client(None)
        assert client.token_endpoint_auth_method == "client_secret_basic"


class TestTokenEndpointClientAuth:
    """The client authenticates as it is registered to, not as whichever method
    happens to be convenient."""

    def _apply(self, method, *, client_id="cid", client_secret="sek"):
        client = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=["https://app/cb"],
            token_endpoint_auth_method=method,
        )
        return _apply_client_auth(client, {"grant_type": "authorization_code"}, {})

    def test_basic_goes_in_the_header_and_not_the_body(self):
        data, headers = self._apply("client_secret_basic")
        expected = base64.b64encode(b"cid:sek").decode()
        assert headers["Authorization"] == f"Basic {expected}"
        assert "client_secret" not in data

    def test_post_goes_in_the_body_and_not_the_header(self):
        data, headers = self._apply("client_secret_post")
        assert data["client_secret"] == "sek"
        assert "Authorization" not in headers

    def test_a_public_client_sends_the_secret_nowhere(self):
        data, headers = self._apply("none")
        assert "client_secret" not in data
        assert "Authorization" not in headers

    def test_credentials_are_percent_encoded_before_base64(self):
        # RFC 6749 §2.3.1 — a secret containing ':' would otherwise decode into
        # the wrong id/secret split at the server.
        _data, headers = self._apply(
            "client_secret_basic",
            client_id="a b",
            client_secret="p:q",
        )
        raw = base64.b64decode(headers["Authorization"].removeprefix("Basic "))
        assert raw.decode() == "a%20b:p%3Aq"

    async def _exchange(self, monkeypatch, method):
        """Run a token exchange; return the token and the request it sent."""
        sent: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(
                200,
                json={"access_token": "at", "token_type": "Bearer"},
            )

        _serve(monkeypatch, handle)
        token = await exchange_code(
            _metadata(),
            OAuthClientInformationFull(
                client_id="cid",
                client_secret="sek",
                redirect_uris=["https://app/cb"],
                token_endpoint_auth_method=method,
            ),
            code="abc",
            server_url="https://mcp.example.com/mcp",
            redirect_uri="https://app/cb",
            code_verifier="v",
        )
        return token, sent[0]

    async def test_the_exchange_authenticates_with_basic(self, monkeypatch):
        # The unit tests above prove the helper; this proves the token leg is
        # wired to it, which is where the secret was previously hard-posted.
        token, request = await self._exchange(monkeypatch, "client_secret_basic")
        assert token.access_token == "at"
        assert request.headers["authorization"].startswith("Basic ")
        assert "client_secret" not in request.content.decode()

    async def test_the_exchange_authenticates_with_post(self, monkeypatch):
        _token, request = await self._exchange(monkeypatch, "client_secret_post")
        assert "client_secret=sek" in request.content.decode()
        assert "authorization" not in request.headers


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
                metadata,
                _client(),
                code="abc",
                server_url="https://mcp.example.com/mcp",
                redirect_uri="https://app/cb",
                code_verifier="v",
            )


async def _allow(_url):
    """``guard_public_url`` stand-in for tests that drive the client directly."""
    return None


def _serve(monkeypatch, handler, *, allow_any_url=False):
    """Route the flow's outbound requests through *handler*.

    Stubs ``guarded_client`` rather than the SDK helpers, so the real request
    builders and response parsers run — the only arrangement under which a
    coroutine left unawaited shows up as a failure rather than as a plausible
    truthy object.
    """
    from app.services.mcp import oauth

    if allow_any_url:

        async def allow(_url):
            return None

        monkeypatch.setattr(oauth, "guard_public_url", allow)
    monkeypatch.setattr(
        oauth,
        "guarded_client",
        lambda **_kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(_streaming(handler)),
        ),
    )


class TestDiscoverAuthorization:
    """Discovery end to end against a stubbed server.

    Cover for the happy path the SSRF cases never reach: it runs entirely
    through ``async def`` SDK helpers, whose responses are meaningless until
    awaited.
    """

    _AS_METADATA = {
        "issuer": "https://as.example.com",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
    }

    _RESOURCE = {
        "resource": "https://mcp.example.com/mcp",
        "authorization_servers": ["https://as.example.com"],
    }

    def _handler(self, *, resource=..., authorization=None):
        """Serve a conformant pair unless a test overrides one half."""
        resource = self._RESOURCE if resource is ... else resource
        authorization = authorization or self._AS_METADATA

        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "oauth-protected-resource" in path:
                return (
                    httpx.Response(404) if resource is None else httpx.Response(200, json=resource)
                )
            if "oauth-authorization-server" in path or "openid-configuration" in path:
                return httpx.Response(200, json=authorization)
            return httpx.Response(404)

        return handle

    async def test_returns_the_authorization_server_metadata(self, monkeypatch):
        _serve(monkeypatch, self._handler(), allow_any_url=True)
        discovered = await discover_authorization("https://mcp.example.com/mcp")
        assert isinstance(discovered.metadata, OAuthMetadata)
        assert str(discovered.metadata.token_endpoint) == "https://as.example.com/token"

    async def test_the_discovery_client_declines_connection_reuse(self, monkeypatch):
        # Discovery is the one client that crosses hostnames, and the pinning
        # transport pools connections by resolved IP — so two hostnames behind
        # one CDN IP could reuse a TLS connection verified for the other name.
        # ``Connection: close`` is what rules that out; pin it here so a
        # refactor of ``_discover`` cannot quietly drop it.
        from app.services.mcp import oauth

        built_with: list[dict] = []

        def capture(**kwargs):
            built_with.append(kwargs)
            return httpx.AsyncClient(
                headers=kwargs.get("headers"),
                transport=httpx.MockTransport(_streaming(self._handler())),
            )

        monkeypatch.setattr(oauth, "guard_public_url", _allow)
        monkeypatch.setattr(oauth, "guarded_client", capture)

        await discover_authorization("https://mcp.example.com/mcp")

        assert built_with, "discovery never built a client"
        assert built_with[0].get("headers") == {"Connection": "close"}

    async def test_takes_the_scopes_the_protected_resource_declares(self, monkeypatch):
        # Also the proof that the RFC 9728 document was parsed rather than left
        # as a coroutine: unparsed, it contributes no scopes at all.
        _serve(
            monkeypatch,
            self._handler(
                resource={
                    "resource": "https://mcp.example.com/mcp",
                    "authorization_servers": ["https://as.example.com"],
                    "scopes_supported": ["files:read", "files:write"],
                }
            ),
            allow_any_url=True,
        )
        discovered = await discover_authorization("https://mcp.example.com/mcp")
        assert discovered.scope_for(None) == "files:read files:write"

    async def test_refuses_a_server_whose_metadata_omits_pkce(self, monkeypatch):
        # The MUST enforced against a real discovery run, not just the helper.
        without_pkce = {
            key: value
            for key, value in self._AS_METADATA.items()
            if key != "code_challenge_methods_supported"
        }
        _serve(
            monkeypatch,
            self._handler(authorization=without_pkce),
            allow_any_url=True,
        )
        with pytest.raises(ConnectorOAuthError):
            await discover_authorization("https://mcp.example.com/mcp")

    async def test_uses_the_resource_metadata_url_the_challenge_names(self, monkeypatch):
        # MCP lets a server publish its RFC 9728 document anywhere and point at
        # it from the 401. No ``.well-known`` guess would find this one.
        served = "https://mcp.example.com/tenants/acme/resource-metadata"

        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://mcp.example.com/mcp":
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": f'Bearer resource_metadata="{served}"',
                    },
                )
            if url == served:
                return httpx.Response(
                    200,
                    json={
                        "resource": "https://mcp.example.com/mcp",
                        "authorization_servers": ["https://as.example.com"],
                        "scopes_supported": ["files:read", "files:write"],
                    },
                )
            if "oauth-protected-resource" in request.url.path:
                return httpx.Response(404)
            return httpx.Response(200, json=self._AS_METADATA)

        _serve(monkeypatch, handle, allow_any_url=True)
        discovered = await discover_authorization("https://mcp.example.com/mcp")
        assert discovered.scopes_supported == ["files:read", "files:write"]

    async def test_the_challenge_scope_outranks_the_advertised_set(self, monkeypatch):
        # The challenge names what *this* resource needs; scopes_supported is
        # everything the authorization server might ever grant.
        def handle(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://mcp.example.com/mcp":
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": 'Bearer scope="files:read"',
                    },
                )
            if "oauth-protected-resource" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "resource": "https://mcp.example.com/mcp",
                        "authorization_servers": ["https://as.example.com"],
                        "scopes_supported": ["files:read", "files:write", "admin"],
                    },
                )
            return httpx.Response(200, json=self._AS_METADATA)

        _serve(monkeypatch, handle, allow_any_url=True)
        discovered = await discover_authorization("https://mcp.example.com/mcp")

        assert discovered.scope_for(None) == "files:read"
        # An operator asking for more gets more — but never at the cost of what
        # the server said it needs, which MCP makes authoritative.
        assert discovered.scope_for("custom") == "files:read custom"

    async def test_a_server_that_does_not_challenge_still_discovers(self, monkeypatch):
        # Both routes are required; the well-known one must stand alone.
        _serve(monkeypatch, self._handler(), allow_any_url=True)
        discovered = await discover_authorization("https://mcp.example.com/mcp")
        assert discovered.challenge_scope is None
        assert isinstance(discovered.metadata, OAuthMetadata)

    async def test_a_server_that_streams_instead_of_challenging_is_dropped(self, monkeypatch):
        # The challenge read sends the SSE-open shape, and a server may answer
        # it with a stream rather than a 401. httpx bounds each read, not the
        # request, so its heartbeats would hold the authorize endpoint open for
        # as long as it kept writing.
        from app.services.mcp import oauth

        monkeypatch.setattr(oauth, "_HTTP_TIMEOUT", 0.05)
        answer = self._handler()

        async def handle(request: httpx.Request) -> httpx.Response:
            if ".well-known" not in request.url.path:
                await asyncio.sleep(5)
            return answer(request)

        _serve(monkeypatch, handle, allow_any_url=True)

        started = time.monotonic()
        discovered = await discover_authorization("https://mcp.example.com/mcp")

        assert time.monotonic() - started < 1.0
        assert isinstance(discovered.metadata, OAuthMetadata)

    async def test_only_the_servers_own_answers_are_shrugged_off(self):
        # Best-effort covers what the server says, not our own refusal to talk
        # to it — the pinning transport rejects a host that resolves private
        # only at request time. Reporting that as "this server issued no
        # challenge" would file our error under the server's behaviour.
        client = AsyncMock()
        # The refusal comes from the pinning transport, so it surfaces on the
        # send — where the bounded read now issues it.
        client.send.side_effect = InvalidConnectorUrlError("resolves to private")
        client.build_request = httpx.Client().build_request

        with pytest.raises(InvalidConnectorUrlError):
            await _read_challenge(client, "https://mcp.example.com/mcp")

    async def test_a_redirect_on_one_candidate_does_not_end_discovery(
        self,
        monkeypatch,
    ):
        # The guarded client refuses to follow redirects, so a provider whose
        # legacy well-known path 301s to its canonical one hands us the 301
        # itself — and taking that for "this server is broken" would bury the
        # OIDC candidate behind it, making the Connector unaddable.
        def handle(request: httpx.Request) -> httpx.Response:
            if "oauth-protected-resource" in request.url.path:
                return httpx.Response(200, json=self._RESOURCE)
            if "oauth-authorization-server" in request.url.path:
                return httpx.Response(301, headers={"location": "https://elsewhere"})
            if "openid-configuration" in request.url.path:
                return httpx.Response(200, json=self._AS_METADATA)
            return httpx.Response(404)

        _serve(monkeypatch, handle, allow_any_url=True)

        discovered = await discover_authorization("https://mcp.example.com/mcp")

        assert str(discovered.metadata.token_endpoint) == "https://as.example.com/token"

    async def test_a_server_error_still_ends_it(self, monkeypatch):
        # The other half of the same rule: a 5XX says the endpoint itself is
        # unwell, and the candidates behind it are no more likely to answer.
        attempts: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            if "oauth-protected-resource" in request.url.path:
                return httpx.Response(200, json=self._RESOURCE)
            if ".well-known" not in request.url.path:
                return httpx.Response(404)  # the challenge read, not a candidate
            attempts.append(request.url.path)
            return httpx.Response(503)

        _serve(monkeypatch, handle, allow_any_url=True)

        with pytest.raises(ConnectorOAuthError):
            await discover_authorization("https://mcp.example.com/mcp")

        assert len(attempts) == 1

    async def test_the_issuer_is_recorded_as_published(self, monkeypatch):
        # Not as pydantic renders it: ``AnyUrl`` gives a bare origin a trailing
        # slash, and RFC 9207 forbids normalising either side before
        # comparison — so recording the parsed value would reject every
        # provider whose issuer is a bare origin.
        bare = dict(self._AS_METADATA, issuer="https://as.example.com")
        _serve(
            monkeypatch,
            self._handler(
                resource={
                    "resource": "https://mcp.example.com/mcp",
                    "authorization_servers": ["https://as.example.com"],
                },
                authorization=bare,
            ),
            allow_any_url=True,
        )

        discovered = await discover_authorization("https://mcp.example.com/mcp")

        assert str(discovered.metadata.issuer) == "https://as.example.com/"
        assert discovered.issuer_as_published == "https://as.example.com"

    async def test_the_iss_support_flag_survives_parsing(self, monkeypatch):
        # The SDK's model does not carry it, so it has to come off the document.
        advertising = dict(self._AS_METADATA, authorization_response_iss_parameter_supported=True)
        _serve(
            monkeypatch,
            self._handler(authorization=advertising),
            allow_any_url=True,
        )

        discovered = await discover_authorization("https://mcp.example.com/mcp")

        assert discovered.iss_parameter_supported is True

    async def test_a_slow_server_cannot_hold_the_endpoint_open(self, monkeypatch):
        # ``httpx.Timeout`` bounds each read, so a server answering a byte at a
        # time never trips it — and this runs while a request and its database
        # session are held. One discovery is six requests at worst, so the
        # bound has to cover the sequence rather than each of its steps.
        from app.services.mcp import oauth

        monkeypatch.setattr(oauth, "_DISCOVERY_TIMEOUT", 0.05)

        async def never_answers(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200, json=self._RESOURCE)

        _serve(monkeypatch, never_answers, allow_any_url=True)

        started = time.monotonic()
        with pytest.raises(ConnectorOAuthError):
            await discover_authorization("https://mcp.example.com/mcp")

        assert time.monotonic() - started < 1.0

    async def test_a_document_naming_someone_else_is_ignored(self, monkeypatch):
        # A server may publish metadata claiming another party's resource and
        # pointing at their authorization server. Taking it would send the
        # user's browser to a login page they rightly trust, to authorize a
        # grant we then hold against a different host.
        impersonating = {
            "resource": "https://bank.example.com",
            "authorization_servers": ["https://bank-as.example.com"],
        }

        def handle(request: httpx.Request) -> httpx.Response:
            if "oauth-protected-resource" in request.url.path:
                return httpx.Response(200, json=impersonating)
            if request.url.host == "bank-as.example.com":
                raise AssertionError("must not follow the impersonated server")
            return httpx.Response(
                200,
                json=dict(self._AS_METADATA, issuer="https://mcp.example.com"),
            )

        _serve(monkeypatch, handle, allow_any_url=True)

        # The impersonated document is discarded, which leaves no document at
        # all — and nothing else names an authorization server. The handler
        # asserts the one it tried to point at is never contacted.
        with pytest.raises(ConnectorOAuthError):
            await discover_authorization("https://mcp.example.com/mcp")

    async def test_a_trailing_slash_is_a_different_issuer(self, monkeypatch):
        # RFC 8414 §3.3 says identical, and ``/tenant`` is not ``/tenant/`` —
        # an authorization server routes them to different tenants. Trimming
        # either side to be helpful merges two identities.
        resource = dict(
            self._RESOURCE,
            authorization_servers=["https://as.example.com/tenant/"],
        )
        served = dict(self._AS_METADATA, issuer="https://as.example.com/tenant")
        _serve(
            monkeypatch,
            self._handler(resource=resource, authorization=served),
            allow_any_url=True,
        )

        with pytest.raises(ConnectorOAuthError):
            await discover_authorization("https://mcp.example.com/mcp")

    async def test_metadata_claiming_a_different_issuer_is_ignored(self, monkeypatch):
        # RFC 8414 §3.3. A resource document naming ``as.example.com`` whose
        # well-known answers for a *different* tenant would otherwise send the
        # browser, the authorization code, and the exchange to that tenant.
        wrong_tenant = dict(self._AS_METADATA, issuer="https://as.example.com/other")
        _serve(
            monkeypatch,
            self._handler(authorization=wrong_tenant),
            allow_any_url=True,
        )

        with pytest.raises(ConnectorOAuthError):
            await discover_authorization("https://mcp.example.com/mcp")

    async def test_accepts_a_scoped_dcr_namespace_for_a_shared_issuer(
        self,
        monkeypatch,
    ):
        resource = dict(
            self._RESOURCE,
            authorization_servers=["https://as.example.com/tenant"],
        )
        shared_issuer = dict(
            self._AS_METADATA,
            issuer="https://as.example.com",
            registration_endpoint="https://as.example.com/tenant/dcr/register",
        )
        _serve(
            monkeypatch,
            self._handler(resource=resource, authorization=shared_issuer),
            allow_any_url=True,
        )

        discovered = await discover_authorization("https://mcp.example.com/mcp")

        assert str(discovered.metadata.issuer) == "https://as.example.com/"

    async def test_refuses_a_scoped_alias_with_registration_outside_its_namespace(
        self,
        monkeypatch,
    ):
        resource = dict(
            self._RESOURCE,
            authorization_servers=["https://as.example.com/tenant"],
        )
        mismatched_registration = dict(
            self._AS_METADATA,
            issuer="https://as.example.com",
            registration_endpoint="https://as.example.com/other/dcr/register",
        )
        _serve(
            monkeypatch,
            self._handler(
                resource=resource,
                authorization=mismatched_registration,
            ),
            allow_any_url=True,
        )

        with pytest.raises(ConnectorOAuthError):
            await discover_authorization("https://mcp.example.com/mcp")

    async def test_prefers_an_exact_issuer_over_a_scoped_alias(self, monkeypatch):
        """The alias is a last resort, not the first answer that fits.

        The SDK offers three metadata URLs. The first two hang the tenant off
        ``/.well-known/…``; the third puts it in front, which is where a
        provider that scopes its issuer per tenant publishes a document
        claiming that exact issuer. Taking the alias as soon as it appears
        would never reach it — and would keep taking it after the provider
        started conforming.
        """
        resource = dict(
            self._RESOURCE,
            authorization_servers=["https://as.example.com/tenant"],
        )
        shared_issuer = dict(
            self._AS_METADATA,
            issuer="https://as.example.com",
            registration_endpoint="https://as.example.com/tenant/dcr/register",
        )
        exact_issuer = dict(
            self._AS_METADATA,
            issuer="https://as.example.com/tenant",
            registration_endpoint="https://as.example.com/tenant/dcr/register",
        )

        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "oauth-protected-resource" in path:
                return httpx.Response(200, json=resource)
            # Only the third candidate puts the tenant before ``.well-known``.
            if path.startswith("/tenant/"):
                return httpx.Response(200, json=exact_issuer)
            if "oauth-authorization-server" in path or "openid-configuration" in path:
                return httpx.Response(200, json=shared_issuer)
            return httpx.Response(404)

        _serve(monkeypatch, handle, allow_any_url=True)

        discovered = await discover_authorization("https://mcp.example.com/mcp")

        assert str(discovered.metadata.issuer) == "https://as.example.com/tenant"

    async def test_refuses_a_scoped_alias_whose_token_endpoint_left_the_origin(
        self,
        monkeypatch,
    ):
        """The registration endpoint is not the only one worth pinning.

        The browser is sent to ``authorization_endpoint`` and the code is spent
        at ``token_endpoint``; an alias that moved either off the origin the
        resource named is not a tenant of it.
        """
        resource = dict(
            self._RESOURCE,
            authorization_servers=["https://as.example.com/tenant"],
        )
        exfiltrating_token_endpoint = dict(
            self._AS_METADATA,
            issuer="https://as.example.com",
            registration_endpoint="https://as.example.com/tenant/dcr/register",
            token_endpoint="https://elsewhere.example.com/token",
        )
        _serve(
            monkeypatch,
            self._handler(
                resource=resource,
                authorization=exfiltrating_token_endpoint,
            ),
            allow_any_url=True,
        )

        with pytest.raises(ConnectorOAuthError):
            await discover_authorization("https://mcp.example.com/mcp")

    async def test_refuses_a_plaintext_authorization_endpoint(self, monkeypatch):
        # The one endpoint no transport of ours protects: the browser is sent
        # there carrying client_id, state and the PKCE challenge.
        plaintext = dict(
            self._AS_METADATA,
            authorization_endpoint="http://login.example.com/authorize",
        )
        _serve(
            monkeypatch,
            self._handler(authorization=plaintext),
            allow_any_url=True,
        )
        with pytest.raises(ConnectorOAuthError, match="https"):
            await discover_authorization("https://mcp.example.com/mcp")

    async def test_reports_a_server_with_no_metadata_at_all(self, monkeypatch):
        _serve(
            monkeypatch,
            lambda _request: httpx.Response(404),
            allow_any_url=True,
        )
        with pytest.raises(ConnectorOAuthError):
            await discover_authorization("https://mcp.example.com/mcp")

    async def test_a_server_publishing_no_resource_document_is_refused(self, monkeypatch):
        # A server nominating its own origin is the guess RFC 9728 exists to
        # remove, and the spec's discovery sequence ends this branch at "abort
        # or use pre-configured values". Serving perfectly good authorization
        # metadata at that origin does not make it discoverable.
        its_own = dict(
            self._AS_METADATA,
            issuer="https://mcp.example.com",
            token_endpoint="https://mcp.example.com/token",
        )
        _serve(
            monkeypatch,
            self._handler(resource=None, authorization=its_own),
            allow_any_url=True,
        )

        with pytest.raises(ConnectorOAuthError, match="Protected Resource"):
            await discover_authorization("https://mcp.example.com/mcp")


class TestDynamicClientRegistration:
    """DCR is the fallback when the server advertises neither a manual client
    nor CIMD — and the branch every other ``ensure_client`` test skips."""

    async def test_a_server_that_does_not_advertise_dcr_is_not_posted_to(self):
        # The SDK's request builder does not check: handed metadata with no
        # ``registration_endpoint`` it guesses ``{issuer}/register``, which for a
        # provider that simply does not do DCR is an ordinary web route on their
        # site. GitHub's remote MCP server is the case in point — it wants a
        # pre-registered OAuth app, so the guess POSTs a registration document
        # at https://github.com/register and collects a 422.
        from app.services.mcp.oauth import ensure_client

        no_dcr = OAuthMetadata(
            issuer="https://github.com",
            authorization_endpoint="https://github.com/login/oauth/authorize",
            token_endpoint="https://github.com/login/oauth/access_token",
            response_types_supported=["code"],
        )

        with pytest.raises(ConnectorOAuthError, match="dynamic client registration"):
            await ensure_client(
                no_dcr,
                redirect_uri="https://app/cb",
                scope=None,
                client_name=CLIENT_NAME,
            )

    def _registration_metadata(self) -> OAuthMetadata:
        return OAuthMetadata(
            issuer="https://as.example.com",
            authorization_endpoint="https://as.example.com/authorize",
            token_endpoint="https://as.example.com/token",
            response_types_supported=["code"],
            registration_endpoint="https://as.example.com/register",
        )

    async def test_returns_the_client_the_server_registered(self, monkeypatch):
        registered = {
            "client_id": "generated-cid",
            "client_secret": "generated-secret",
            "redirect_uris": ["https://app/cb"],
            "token_endpoint_auth_method": "client_secret_basic",
        }
        _serve(monkeypatch, lambda _request: httpx.Response(201, json=registered))

        client = await ensure_client(
            self._registration_metadata(),
            redirect_uri="https://app/cb",
            scope=None,
            client_name=CLIENT_NAME,
        )

        assert isinstance(client, OAuthClientInformationFull)
        assert client.client_id == "generated-cid"
        # We asked for POST; the server substituted Basic, as RFC 7591 §3.2.1
        # lets it. Reading the answer back is the point of the round-trip.
        assert client.token_endpoint_auth_method == "client_secret_basic"

    async def test_registration_asks_for_the_method_the_server_declares(self, monkeypatch):
        # A registration endpoint that only does Basic rejects an unsupported
        # request outright rather than correcting it, so asking blindly for
        # POST locks us out of those servers entirely.
        sent: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(
                201,
                json={
                    "client_id": "cid",
                    "redirect_uris": ["https://app/cb"],
                    "token_endpoint_auth_method": "client_secret_basic",
                },
            )

        _serve(monkeypatch, handle)
        basic_only = self._registration_metadata().model_copy(
            update={"token_endpoint_auth_methods_supported": ["client_secret_basic"]}
        )

        await ensure_client(
            basic_only,
            redirect_uri="https://app/cb",
            scope=None,
            client_name=CLIENT_NAME,
        )

        assert '"token_endpoint_auth_method":"client_secret_basic"' in (
            sent[0].content.decode().replace(" ", "")
        )

    async def test_an_omitted_auth_method_is_pinned_before_the_row_is_stored(self, monkeypatch):
        # Left as None the two readers disagree: this exchange would take the
        # RFC default (Basic) while a later rotation reads it as "send no
        # secret", so an authorization that worked stops being renewable.
        registered = {
            "client_id": "generated-cid",
            "client_secret": "generated-secret",
            "redirect_uris": ["https://app/cb"],
        }
        _serve(monkeypatch, lambda _request: httpx.Response(201, json=registered))

        client = await ensure_client(
            self._registration_metadata(),
            redirect_uri="https://app/cb",
            scope=None,
            client_name=CLIENT_NAME,
        )

        assert client.token_endpoint_auth_method == "client_secret_basic"

    async def test_a_rejected_registration_says_to_configure_a_client(
        self,
        monkeypatch,
    ):
        _serve(monkeypatch, lambda _request: httpx.Response(400, text="nope"))
        with pytest.raises(ConnectorOAuthError, match="Client ID"):
            await ensure_client(
                self._registration_metadata(),
                redirect_uri="https://app/cb",
                scope=None,
                client_name=CLIENT_NAME,
            )


class TestBuildOAuthConnector:
    """The row a custom OAuth Connector is created as.

    Regression cover for the create path itself: it builds an ``MCPServer``
    positionally by keyword, so a column rename that misses it raises
    ``TypeError`` at request time while every other test still passes.
    """

    async def test_a_second_connector_for_one_server_is_refused_before_registering(
        self,
        monkeypatch,
    ):
        # Dynamic registration mints a client at the provider. One minted for a
        # row that then 409s on the index is orphaned — never stored, so never
        # revocable — and a user re-adding the same server a few times would
        # leave one behind each time. The index is on the URL now: the server
        # is what a Connector is, and the name is only its label.
        from app.api.public import mcp_server as endpoint
        from app.errors import ConnectorUrlConflictError
        from app.models.mcp_server import MCPServerAuthType
        from app.schemas.mcp_server import ConnectorCreate

        registered: list[object] = []

        async def never(**kwargs):
            registered.append(kwargs)

        monkeypatch.setattr(endpoint, "_register_oauth_client", never)

        db = _session()
        db.execute.return_value = MagicMock(first=MagicMock(return_value=(1,)))

        with pytest.raises(ConnectorUrlConflictError):
            await endpoint._build_oauth_connector(
                db,
                ConnectorCreate(
                    name="Notion",
                    url="https://mcp.notion.com/mcp",
                    auth_type=MCPServerAuthType.OAUTH,
                ),
                user_id=42,
            )

        assert registered == []

    async def test_builds_a_disconnected_row(self, monkeypatch):
        from app.api.public import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType
        from app.schemas.mcp_server import ConnectorCreate

        async def fake_register(**_kwargs):
            return "encrypted-client", {
                "authorization_server": {"issuer": "https://as/"},
            }

        monkeypatch.setattr(endpoint, "_register_oauth_client", fake_register)

        connector = await endpoint._build_oauth_connector(
            _db_with_no_connector_named_that(),
            ConnectorCreate(
                name="Notion",
                url="https://mcp.notion.com/mcp",
                auth_type=MCPServerAuthType.OAUTH,
                oauth_scope="read",
            ),
            user_id=42,
        )

        assert connector.user_id == 42
        assert connector.auth_type == MCPServerAuthType.OAUTH
        assert connector.oauth_client_encrypted == "encrypted-client"
        # Stored with it: a client belongs to the server that issued it, and a
        # later authorization has to be able to notice that server changing.
        assert connector.oauth_metadata == {
            "authorization_server": {"issuer": "https://as/"},
        }
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

    _METADATA_URL = "https://api.example.com/api/connectors/client-metadata"

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
            redirect_uri="https://app/cb",
            scope="read",
            client_name=CLIENT_NAME,
            client_metadata_url=self._METADATA_URL,
        )
        assert client.client_id == self._METADATA_URL
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
                redirect_uri="https://app/cb",
                scope=None,
                client_name=CLIENT_NAME,
                client_metadata_url=self._METADATA_URL,
            )

    async def test_a_manual_client_id_still_wins_over_cimd(self):
        # An operator who configured a real OAuth app must keep using it.
        client = await ensure_client(
            self._metadata_supporting_cimd(),
            redirect_uri="https://app/cb",
            scope=None,
            client_name=CLIENT_NAME,
            client_id="manual-cid",
            client_secret="sek",
            client_metadata_url=self._METADATA_URL,
        )
        assert client.client_id == "manual-cid"

    async def test_rejects_a_metadata_url_that_cannot_be_a_client_id(self):
        # Must be https with a non-root path, or the AS cannot resolve it.
        for bad in (
            "http://api.example.com/client-metadata",
            "https://api.example.com/",
        ):
            with pytest.raises(ConnectorOAuthError):
                await ensure_client(
                    self._metadata_supporting_cimd(),
                    redirect_uri="https://app/cb",
                    scope=None,
                    client_name=CLIENT_NAME,
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
                redirect_uri="https://app/cb",
                scope=None,
                client_name=CLIENT_NAME,
                client_metadata_url="http://not-https/root",
            )

    def test_document_echoes_its_own_url_as_client_id(self):
        # Binding the id to the URL is what stops the document being
        # republished elsewhere under a different identity.
        url = "https://api.example.com/api/connectors/client-metadata"
        doc = client_metadata_document(url, redirect_uri="https://app/cb", client_name=CLIENT_NAME)
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
            redirect_uri="https://app/cb",
            scope="read",
            client_name=CLIENT_NAME,
            client_metadata_url=url,
        )
        published = client_metadata_document(
            url, redirect_uri="https://app/cb", client_name=CLIENT_NAME
        )
        assert published == authorized.model_dump(mode="json", exclude_none=True)
        assert authorized == cimd_client(
            url, redirect_uri="https://app/cb", client_name=CLIENT_NAME
        )


class TestClientMetadataEndpoint:
    """The document is served at the very URL it names as ``client_id``."""

    async def _get(self, monkeypatch, *, metadata_url: str, redirect_uri: str):
        from app.api.public import mcp_server as endpoint

        monkeypatch.setattr(endpoint.settings, "mcp_client_metadata_url", metadata_url)
        monkeypatch.setattr(endpoint.settings, "mcp_oauth_redirect_uri", redirect_uri)
        return await endpoint.get_client_metadata()

    async def test_serves_the_document_when_configured(self, monkeypatch):
        url = "https://api.example.com/api/connectors/client-metadata"
        doc = await self._get(
            monkeypatch,
            metadata_url=url,
            redirect_uri="https://app/cb",
        )
        assert doc["client_id"] == url
        assert doc["redirect_uris"] == ["https://app/cb"]

    async def test_404s_when_cimd_is_not_configured(self, monkeypatch):
        # Publishing an identity that names a redirect URI we may not serve is
        # worse than publishing nothing.
        with pytest.raises(HTTPException) as caught:
            await self._get(monkeypatch, metadata_url="", redirect_uri="https://app/cb")
        assert caught.value.status_code == 404

    async def test_fails_loudly_when_the_redirect_uri_is_missing(self, monkeypatch):
        # A document advertising no redirect URI would strand every flow. 500,
        # not the 502 its neighbours carry: this is the deployment's own
        # omission, so "the provider is having trouble, try again" is both
        # untrue and unactionable.
        with pytest.raises(OAuthNotConfiguredError) as caught:
            await self._get(
                monkeypatch,
                metadata_url="https://api.example.com/api/connectors/client-metadata",
                redirect_uri="",
            )

        assert caught.value.status_code == 500
        # The operator needs the setting's name; the caller must not be handed
        # the shape of a config they have no business knowing.
        assert "mcp_oauth_redirect_uri" not in caught.value.message


class TestScopeSelectionStrategy:
    """MCP's scope-selection strategy: a general-purpose client has no domain
    knowledge to pick scopes with, so it asks for what the server declares."""

    def _discovered(self, scopes, *, challenge=None, offline=False):
        metadata = OAuthMetadata(
            issuer="https://as.example.com",
            authorization_endpoint="https://as.example.com/authorize",
            token_endpoint="https://as.example.com/token",
            response_types_supported=["code"],
            # SEP-2207 puts ``offline_access`` in the *authorization server's*
            # metadata; a conformant resource server never advertises it.
            scopes_supported=["offline_access"] if offline else None,
        )
        return DiscoveredAuthorization(
            metadata=metadata,
            resource=ProtectedResourceMetadata(
                resource="https://mcp.example.com/mcp",
                authorization_servers=["https://as.example.com"],
                scopes_supported=scopes,
            ),
            challenge_scope=challenge,
        )

    def test_uses_the_scopes_the_server_declares(self):
        assert self._discovered(["files:read", "files:write"]).scope_for(None) == (
            "files:read files:write"
        )

    def test_a_configured_scope_narrows_the_declared_set(self):
        # The advertised set is what the spec falls back to for want of domain
        # knowledge. An operator who named the scopes supplied that knowledge,
        # so unioning would turn a deliberate narrowing into a widening.
        assert self._discovered(["files:read", "admin"]).scope_for("files:read") == ("files:read")

    def test_omits_scope_when_neither_is_available(self):
        # Omitting lets the server apply its own default rather than us
        # inventing a scope string it may reject.
        assert self._discovered(None).scope_for(None) is None
        assert self._discovered([]).scope_for(None) is None

    def test_a_refresh_token_is_asked_for_when_the_server_offers_one(self):
        # SEP-2207: without ``offline_access`` the grant carries no refresh
        # token, so every access-token expiry becomes a browser round-trip —
        # the gap most MCP clients still ship with.
        discovered = self._discovered(["files:read"], offline=True)
        assert discovered.scope_for(None) == "files:read offline_access"

    def test_a_challenge_scope_does_not_cost_the_refresh_token(self):
        # A 401 challenge names resource scopes and nothing else, so taking it
        # verbatim silently drops the request for a refresh token.
        discovered = self._discovered(
            ["files:read", "files:write"],
            challenge="files:read",
            offline=True,
        )
        assert discovered.scope_for(None) == "files:read offline_access"

    def test_a_configured_scope_cannot_widen_past_what_is_advertised(self):
        # Narrowing is the point; the operator can only ask for less than the
        # advertised set, or for names the server never listed at their own
        # risk — either way the challenge, when there is one, still stands.
        narrowed = self._discovered(["a", "b", "c"], challenge="a")
        assert narrowed.scope_for("b") == "a b"

    def test_a_configured_scope_never_displaces_the_challenge(self):
        # The failure this prevents: authorization succeeds, and the token
        # cannot touch the resource it was obtained for.
        discovered = self._discovered(
            ["files:read", "files:write"],
            challenge="files:read",
        )
        assert discovered.scope_for("custom") == "files:read custom"

    def test_overlapping_scopes_are_not_repeated(self):
        discovered = self._discovered(None, challenge="files:read files:write")
        assert discovered.scope_for("files:read") == "files:read files:write"

    def test_it_is_not_requested_when_the_server_never_offered_it(self):
        # Asking for an unadvertised scope can have the whole request rejected.
        discovered = self._discovered(["files:read"])
        assert discovered.scope_for(None) == "files:read"
        assert discovered.scope_for("custom") == "custom"


class TestResponseMapping:
    """``_to_response`` builds via ``model_validate``, which maps by attribute
    name — so a schema field renamed away from its column serializes as the
    default instead of failing, and the UI silently loses the state."""

    def test_reauthorization_flag_survives_the_orm_to_schema_hop(self):
        from datetime import datetime

        from app.api.public.mcp_server import _to_response
        from app.models.mcp_server import MCPServer, MCPServerAuthType

        now = datetime(2026, 7, 28)
        connector = MCPServer(
            id=1,
            user_id=7,
            name="Notion",
            url="https://mcp.notion.com/mcp",
            auth_type=MCPServerAuthType.OAUTH,
            oauth_tokens_encrypted="enc",
            connected=True,
            oauth_reauthorization_required=True,
            last_connect_failed=True,
            # NOT NULL columns: a loaded row always has these, but SQLAlchemy
            # applies its Python-side defaults at flush, not at construction.
            tool_count=0,
            disabled_tools=[],
            created_at=now,
            updated_at=now,
            advertised_icons=[{"src": "https://cdn.example.com/notion.png"}],
        )
        response = _to_response(connector)

        assert response.oauth_reauthorization_required is True
        assert response.last_connect_failed is True
        assert response.disabled_tools == []
        assert [icon.src for icon in response.advertised_icons] == [
            "https://cdn.example.com/notion.png"
        ]
        assert response.authorized is True  # tokens present, but the grant is dead

    def test_a_reachable_probe_clears_the_unreachable_flag(self):
        # The user who fixes a broken URL and hits refresh must see the badge
        # go at once, not after the next turn.
        from app.api.public.mcp_server import _apply_probe
        from app.models.mcp_server import MCPServer, MCPServerAuthType
        from app.services.mcp.probe import ProbeResult

        connector = MCPServer(
            id=1,
            user_id=7,
            name="c",
            url="https://mcp.example.com/mcp",
            auth_type=MCPServerAuthType.BEARER,
            last_connect_failed=True,
        )
        _apply_probe(
            connector,
            ProbeResult(server_name="c", tools=[{"name": "t"}]),
        )

        assert connector.last_connect_failed is False
        assert connector.tool_count == 1


class TestProbeCredential:
    """How a saved Connector is authenticated when re-listing its tools."""

    def _connector(self, **over):
        from app.models.mcp_server import MCPServer, MCPServerAuthType

        base = dict(
            id=1,
            user_id=7,
            name="c",
            url="https://mcp.example.com/mcp",
            auth_type=MCPServerAuthType.NONE,
        )
        base.update(over)
        return MCPServer(**base)

    def test_non_oauth_uses_its_own_decrypted_credential(self, monkeypatch):
        from app.api.public import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType

        monkeypatch.setattr(endpoint, "_decrypt_or_none", lambda ciphertext: "tok")
        auth_type, credential = endpoint._probe_credential(
            self._connector(
                auth_type=MCPServerAuthType.BEARER,
                encrypted_credential="e",
            )
        )
        assert (auth_type, credential) == (MCPServerAuthType.BEARER, "tok")

    def test_oauth_probes_as_a_bearer_carrying_the_access_token(self, monkeypatch):
        # The MCP server takes the token the same way either route, and
        # ``probe`` has no OAuth path of its own — same trick the callback uses.
        from mcp.shared.auth import OAuthToken

        from app.api.public import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType

        tokens = OAuthToken(access_token="at", token_type="Bearer")
        monkeypatch.setattr(
            endpoint, "open_secret", lambda ciphertext, **_: tokens.model_dump_json()
        )
        auth_type, credential = endpoint._probe_credential(
            self._connector(auth_type=MCPServerAuthType.OAUTH, oauth_tokens_encrypted="enc")
        )
        assert (auth_type, credential) == (MCPServerAuthType.BEARER, "at")

    def test_an_unauthorized_oauth_connector_refuses_rather_than_probing_bare(self):
        # Probing with no token would be indistinguishable from an
        # unauthenticated probe and would report "no tools" misleadingly.
        from app.api.public import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType

        with pytest.raises(ConnectorOAuthError):
            endpoint._probe_credential(
                self._connector(auth_type=MCPServerAuthType.OAUTH, oauth_tokens_encrypted=None)
            )

    def test_a_token_past_its_lifetime_steers_rather_than_probing_dead(self):
        # A dead token produces a 401 that ``probe`` reports as an unreachable
        # server — blaming the MCP server for a fact already on the row
        # (mirrors the worker's connect-time refusal, ``_usable_unrenewed``).
        from datetime import datetime, timedelta

        from app.api.public import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType

        with pytest.raises(ConnectorOAuthError, match="recorded lifetime"):
            endpoint._probe_credential(
                self._connector(
                    auth_type=MCPServerAuthType.OAUTH,
                    oauth_tokens_encrypted="enc",
                    oauth_tokens_expires_at=(datetime.now(UTC) - timedelta(minutes=1)),
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


class TestRevokingAGrant:
    """Dropping our copy stops us using a grant; only this ends it. Without it
    the user finds an application they disconnected still authorized on their
    account, holding a refresh token that outlives every trace of it here."""

    _METADATA = OAuthMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        revocation_endpoint="https://as.example.com/revoke",
        response_types_supported=["code"],
    )

    def _sent(self, monkeypatch, response: httpx.Response) -> dict:
        captured: dict = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return response

        _serve(monkeypatch, handle, allow_any_url=True)
        return captured

    def _tokens(self, refresh_token: str | None):
        from mcp.shared.auth import OAuthToken

        return OAuthToken(
            access_token="AT",
            token_type="Bearer",
            refresh_token=refresh_token,
        )

    async def test_it_sends_the_refresh_token_to_the_revocation_endpoint(
        self,
        monkeypatch,
    ):
        # The refresh token rather than the access token: RFC 7009 §2.1 asks a
        # server revoking one to invalidate the whole grant family behind it,
        # so this single request ends the access tokens too. Revoking an access
        # token would end that token and leave the refresh token minting more.
        captured = self._sent(monkeypatch, httpx.Response(200))
        client = OAuthClientInformationFull(
            client_id="cid",
            client_secret="sek",
            redirect_uris=["https://app/cb"],
            token_endpoint_auth_method="client_secret_post",
        )

        await revoke_grant(self._METADATA, client, tokens=self._tokens("R0"))

        request = captured["request"]
        assert str(request.url) == "https://as.example.com/revoke"
        body = request.content.decode()
        assert "token=R0" in body
        assert "token_type_hint=refresh_token" in body
        # Authenticated as at the token endpoint (RFC 7009 §2.1), so a
        # confidential client is not silently refused.
        assert "client_secret=sek" in body

    async def test_a_grant_with_no_refresh_token_revokes_its_access_token(
        self,
        monkeypatch,
    ):
        # Some providers issue no refresh token at all; revoking the only token
        # they gave us is the most that can be said.
        captured = self._sent(monkeypatch, httpx.Response(200))

        await revoke_grant(self._METADATA, _client(), tokens=self._tokens(None))

        body = captured["request"].content.decode()
        assert "token=AT" in body
        assert "token_type_hint=access_token" in body

    async def test_an_unrecognised_token_is_success(self, monkeypatch):
        # RFC 7009 §2.2 — already revoked, already expired and never valid all
        # answer 200, and all mean the same thing to us: the provider is not
        # holding it.
        self._sent(monkeypatch, httpx.Response(200))

        await revoke_grant(self._METADATA, _client(), tokens=self._tokens("R0"))

    async def test_a_refusal_is_reported_rather_than_swallowed(self, monkeypatch):
        # The caller decides what a failure is worth; this must not decide for
        # it by returning as though the grant were gone.
        self._sent(monkeypatch, httpx.Response(503, text="down"))

        with pytest.raises(ConnectorOAuthError):
            await revoke_grant(self._METADATA, _client(), tokens=self._tokens("R0"))

    async def test_a_server_offering_no_endpoint_is_not_guessed_at(self):
        # No fallback to the token endpoint or anywhere else: posting a refresh
        # token to a URL that was never advertised for it is how a credential
        # ends up somewhere it was not meant to go.
        with pytest.raises(ConnectorOAuthError):
            await revoke_grant(_metadata(), _client(), tokens=self._tokens("R0"))


class TestOutboundReadsAreBounded:
    """Every URL fetched here is user- or server-supplied, and the process doing
    the fetching is the API serving other users' requests.

    A time bound cannot express this one: an endpoint answering 200 with
    gigabytes as fast as the socket allows arrives well inside the deadline, and
    ``httpx`` buffers the body whole before there is a response object to
    measure — so the buffer lands in the uvicorn heap either way.
    """

    async def _revoke(self):
        # Revocation is the shortest path to one outbound read; the bound it
        # exercises is shared by every fetch in this module.
        await revoke_grant(
            TestRevokingAGrant._METADATA,
            _client(),
            tokens=TestRevokingAGrant()._tokens(None),
        )

    async def test_an_oversize_body_is_refused_rather_than_buffered(
        self,
        monkeypatch,
    ):
        read = 0

        class _Endless(httpx.AsyncByteStream, httpx.SyncByteStream):
            def __iter__(self):
                raise NotImplementedError

            async def __aiter__(self):
                nonlocal read
                while True:
                    read += 64 * 1024
                    yield b"x" * (64 * 1024)

        # Past ``_serve``, which rebuilds every answer around a bounded stream —
        # the point here is a server that never stops sending.
        monkeypatch.setattr(oauth_service, "guard_public_url", _allow)
        monkeypatch.setattr(
            oauth_service,
            "guarded_client",
            lambda **_kwargs: httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, stream=_Endless()),
                ),
            ),
        )

        with pytest.raises(ConnectorOAuthError):
            await self._revoke()

        assert read <= oauth_service._MAX_RESPONSE_BYTES + 64 * 1024

    async def test_an_encoded_body_is_refused_before_it_is_decoded(
        self,
        monkeypatch,
    ):
        # Refusing eventually is not the property: a decoded read also ends in an
        # error, but only after the allocation, and the expansion ratio is the
        # server's to pick. What is asserted is *why* it was refused — the
        # encoding, which can only be seen before a byte is decoded.
        import gzip

        bomb = gzip.compress(b'{"a":"' + b"x" * (40 * 1024 * 1024) + b'"}')
        # Small enough on the wire that a size cap alone would wave it through.
        assert len(bomb) < oauth_service._MAX_RESPONSE_BYTES

        monkeypatch.setattr(oauth_service, "guard_public_url", _allow)
        monkeypatch.setattr(
            oauth_service,
            "guarded_client",
            lambda **_kwargs: httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        headers={"content-encoding": "gzip"},
                        stream=_Unread(bomb),
                    ),
                ),
            ),
        )

        with pytest.raises(ConnectorOAuthError, match="Content-Encoding"):
            await self._revoke()

    async def test_it_asks_for_no_encoding_at_all(self, monkeypatch):
        seen: dict = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["accept_encoding"] = request.headers.get("accept-encoding")
            return httpx.Response(200)

        _serve(monkeypatch, handle, allow_any_url=True)
        await self._revoke()

        assert seen["accept_encoding"] == "identity"


class TestStoringAFreshGrant:
    """Writing a newly authorized grant onto a row. Verbatim, always: renewal
    and its refresh-token carry-forward belong to the worker."""

    def test_the_deadline_is_written_with_the_grant(self, monkeypatch):
        # ``expires_in`` counts from now, so the moment of writing is the only
        # place the absolute deadline is knowable — and it is what renewal reads
        # to decide. A grant stored without one is never renewed.
        from mcp.shared.auth import OAuthToken

        from app.services.mcp import grant

        monkeypatch.setattr(grant, "encrypt_secret", lambda plaintext: "sealed")
        connector = _stored_connector()

        grant.store_tokens(
            connector,
            OAuthToken(access_token="at", token_type="Bearer", expires_in=3600),
        )

        assert connector.oauth_tokens_encrypted == "sealed"
        assert connector.oauth_tokens_expires_at is not None

    def test_a_renewable_grant_with_no_stated_lifetime_gets_a_deadline(
        self,
        monkeypatch,
    ):
        # NULL would read as "valid forever" and leave renewal blind to a grant
        # that has in fact died.
        from mcp.shared.auth import OAuthToken

        from app.services.mcp import grant

        monkeypatch.setattr(grant, "encrypt_secret", lambda plaintext: "sealed")
        connector = _stored_connector()

        grant.store_tokens(
            connector,
            OAuthToken(access_token="at", token_type="Bearer", refresh_token="R0"),
        )

        ahead = (connector.oauth_tokens_expires_at - datetime.now(UTC)).total_seconds()
        assert grant.ASSUMED_TOKEN_LIFETIME - 5 < ahead <= grant.ASSUMED_TOKEN_LIFETIME

    def test_a_grant_nothing_can_renew_is_given_no_deadline(self, monkeypatch):
        # Some servers issue neither a lifetime nor a refresh token. Assuming a
        # deadline there buys nothing and costs a false alarm: renewal would pick
        # the row up an hour later, find nothing to rotate with, and ask the user
        # to re-authorize a token that never expired.
        from mcp.shared.auth import OAuthToken

        from app.services.mcp import grant

        monkeypatch.setattr(grant, "encrypt_secret", lambda plaintext: "sealed")
        connector = _stored_connector()

        grant.store_tokens(
            connector,
            OAuthToken(access_token="at", token_type="Bearer"),
        )

        assert connector.oauth_tokens_expires_at is None

    def test_a_stated_lifetime_is_taken_even_without_a_refresh_token(
        self,
        monkeypatch,
    ):
        # Here the server did say when it dies, and nothing can renew it — so
        # flagging it for re-authorization *ahead* of that moment is right.
        from mcp.shared.auth import OAuthToken

        from app.services.mcp import grant

        monkeypatch.setattr(grant, "encrypt_secret", lambda plaintext: "sealed")
        connector = _stored_connector()

        grant.store_tokens(
            connector,
            OAuthToken(access_token="at", token_type="Bearer", expires_in=120),
        )

        ahead = (connector.oauth_tokens_expires_at - datetime.now(UTC)).total_seconds()
        assert 115 < ahead <= 120

    def test_a_code_exchange_never_inherits_the_old_refresh_token(
        self,
        monkeypatch,
    ):
        # A code exchange establishes a new grant, so an old refresh token has
        # no standing in it — and the new grant may have come from a *different*
        # authorization server, which would then be handed the old one.
        from mcp.shared.auth import OAuthToken

        from app.services.mcp import grant

        stored = OAuthToken(
            access_token="old",
            token_type="Bearer",
            refresh_token="R0",
        )
        written: dict = {}
        monkeypatch.setattr(
            grant,
            "open_secret",
            lambda _c, **_: stored.model_dump_json(),
        )
        monkeypatch.setattr(
            grant,
            "encrypt_secret",
            lambda plaintext: written.setdefault("it", plaintext),
        )
        connector = _stored_connector(oauth_tokens_encrypted="whatever")

        grant.store_tokens(
            connector,
            OAuthToken(access_token="new", token_type="Bearer"),
        )

        assert OAuthToken.model_validate_json(written["it"]).refresh_token is None


class TestOAuthCallbackHealth:
    """Authorization succeeding says nothing about whether the MCP server will
    talk to us — a wrong token audience, or simply down."""

    def _connector(self):
        from app.models.mcp_server import MCPServer, MCPServerAuthType

        return MCPServer(
            id=1,
            user_id=7,
            name="c",
            url="https://mcp.example.com/mcp",
            auth_type=MCPServerAuthType.OAUTH,
            last_connect_failed=False,
        )

    async def _probe_with(self, monkeypatch, outcome):
        from app.api.public import mcp_server as endpoint

        async def fake_probe(*_args, **_kwargs):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(endpoint, "probe", fake_probe)
        connector = _stored_connector()
        await endpoint._probe_after_authorization(connector, "at")
        return connector

    async def test_an_unreachable_server_is_marked(self, monkeypatch):
        from app.errors import ConnectorUnreachableError

        connector = await self._probe_with(
            monkeypatch,
            ConnectorUnreachableError("down"),
        )
        assert connector.last_connect_failed is True

    async def test_it_never_raises_mid_redirect(self, monkeypatch):
        # The grant is already stored and the user is coming back from the
        # provider; failing here would strand a Connector that *is* authorized.
        connector = await self._probe_with(monkeypatch, RuntimeError("boom"))
        assert connector.last_connect_failed is True

    async def test_a_reachable_server_is_recorded_healthy(self, monkeypatch):
        from app.services.mcp.probe import ProbeResult

        connector = await self._probe_with(
            monkeypatch,
            ProbeResult(server_name="c", tools=[{"name": "t"}]),
        )
        assert connector.last_connect_failed is False
        assert connector.tool_count == 1


_NOW = datetime(2026, 7, 29, tzinfo=UTC)


class TestIssuerIdentification:
    """RFC 9207 on the authorization response — the mix-up defence.

    Its precondition is this service's shape: one client, one redirect URI,
    many unrelated authorization servers.
    """

    _RECORDED = "https://as.example.com"

    def _check(self, returned, *, recorded=..., advertised=False):
        require_matching_issuer(
            returned,
            recorded=self._RECORDED if recorded is ... else recorded,
            advertised=advertised,
        )

    def test_a_response_from_another_issuer_is_refused(self):
        # The whole point: without this the code — and the PKCE verifier with
        # it — would be posted to the token endpoint of the server that did
        # *not* issue it, which is how the attacker redeems it at the one that
        # did. PKCE guards a code intercepted without the verifier; here we
        # would hand the verifier over ourselves.
        with pytest.raises(ConnectorOAuthError):
            self._check("https://attacker.example.com")

    def test_the_recorded_issuer_passes(self):
        self._check(self._RECORDED)

    def test_a_trailing_slash_is_not_normalised_away(self):
        # The RFC forbids trailing-slash normalisation before comparison, so
        # this must fail — and it is why the recorded value has to be the
        # string the document published rather than the one pydantic's
        # ``AnyUrl`` canonicalised.
        with pytest.raises(ConnectorOAuthError):
            self._check(self._RECORDED + "/")

    def test_an_advertised_parameter_may_not_go_missing(self):
        with pytest.raises(ConnectorOAuthError):
            self._check(None, advertised=True)

    def test_a_server_that_never_advertised_it_may_omit_it(self):
        self._check(None)

    def test_it_is_compared_even_when_unadvertised(self):
        # The RFC's local-policy provision, which MCP takes: servers emitting
        # ``iss`` before advertising it still get checked.
        with pytest.raises(ConnectorOAuthError):
            self._check("https://attacker.example.com", advertised=False)

    def test_a_flow_started_before_the_check_fails_closed(self):
        # A pending authorization from before the issuer was recorded cannot
        # be validated; it costs one more click, not a silent exchange.
        with pytest.raises(ConnectorOAuthError):
            self._check(self._RECORDED, recorded=None)


class TestCallbackRejectsAMixedUpResponse:
    """The check has to be wired into the callback, not merely available."""

    async def test_the_code_is_never_exchanged(self, monkeypatch):
        import json as json_module

        from app.api.public import mcp_server as endpoint
        from app.rate_limit import limiter
        from app.schemas.mcp_server import OAuthCallbackRequest

        monkeypatch.setattr(limiter, "enabled", False)
        exchanged: list[object] = []

        async def never(*_args, **_kwargs):
            exchanged.append(True)

        monkeypatch.setattr(endpoint.oauth, "exchange_code", never)

        redis = AsyncMock()
        _stub_state_consumption(
            redis,
            json_module.dumps(
                {
                    "connector_id": 1,
                    "user_id": 7,
                    "code_verifier": "v",
                    "issuer": "https://as.example.com",
                    "iss_advertised": True,
                    "discovered": {"authorization_server": {}},
                }
            ),
        )

        with pytest.raises(ConnectorOAuthError):
            await endpoint.oauth_callback(
                request=MagicMock(),
                body=OAuthCallbackRequest(
                    code="c",
                    state="s",
                    iss="https://attacker.example.com",
                ),
                db=_session(),
                redis=redis,
            )

        assert exchanged == []
        # One-shot regardless: a rejected response still burns its state, and
        # burning it is part of the same transaction that read it.
        redis.pipeline.assert_called_once_with(transaction=True)
        redis.pipeline.return_value.delete.assert_called_once()


class TestTwoAuthorizationsInTheAir:
    """Each ``state`` is valid for its whole TTL, so a user who restarts a
    flow — second tab, different account — has two live ones for one Connector.
    The winner must be the one they started last, not the one that finished
    last."""

    def _redis(self, *, current_state: str | None):
        import json as json_module

        redis = AsyncMock()
        pending = json_module.dumps(
            {
                "connector_id": 1,
                "user_id": 7,
                "code_verifier": "v",
                "issuer": None,
                "iss_advertised": False,
                "discovered": {
                    "authorization_server": {
                        "issuer": "https://as.example.com",
                        "authorization_endpoint": "https://as.example.com/authorize",
                        "token_endpoint": "https://as.example.com/token",
                        "response_types_supported": ["code"],
                    }
                },
            }
        )

        _stub_state_consumption(redis, pending)

        async def get(_key):
            return current_state

        redis.get.side_effect = get
        return redis

    def _db(self, *, client: str | None = "sealed-client"):
        """A session that refreshes a loaded object only when asked to.

        Faithful to SQLAlchemy, and the point of the class: ``with_for_update``
        alone bypasses the identity-map *shortcut* and emits the locking query,
        but the row it reads does not overwrite attributes already loaded onto
        the instance the session holds. Only ``populate_existing=True`` does
        that. A double that hands back a fresh object for every ``get`` cannot
        tell a working fence from one that locks the row and then reads its own
        hour-old copy.
        """
        from app.models.mcp_server import MCPServer, MCPServerAuthType

        loaded = MCPServer(
            id=1,
            user_id=7,
            name="c",
            url="https://mcp.example.com/mcp",
            auth_type=MCPServerAuthType.OAUTH,
            oauth_client_encrypted=client,
        )
        db = _session()
        db.identity_map = loaded  # what the session already holds
        db.in_database = loaded  # what the row actually says now

        async def get(_model, _ident, **kwargs):
            if kwargs.get("populate_existing"):
                db.identity_map = db.in_database
            return db.identity_map

        db.get.side_effect = get
        return db

    async def _callback(self, monkeypatch, redis, state, db=None):
        from app.api.public import mcp_server as endpoint
        from app.rate_limit import limiter
        from app.schemas.mcp_server import OAuthCallbackRequest

        monkeypatch.setattr(limiter, "enabled", False)
        return await endpoint.oauth_callback(
            request=MagicMock(),
            body=OAuthCallbackRequest(code="c", state=state, iss=None),
            db=db or AsyncMock(),
            redis=redis,
        )

    async def test_the_older_flow_cannot_land_on_top_of_the_newer(
        self,
        monkeypatch,
    ):
        from app.api.public import mcp_server as endpoint

        exchanged: list[object] = []

        async def never(*_args, **_kwargs):
            exchanged.append(True)

        monkeypatch.setattr(endpoint.oauth, "exchange_code", never)

        with pytest.raises(ConnectorAuthorizationSupersededError) as caught:
            await self._callback(
                monkeypatch,
                self._redis(current_state="newer"),
                "older",
            )

        assert caught.value.status_code == 400
        # Refused before the exchange: burning the newer flow's authorization
        # code would take the Connector down with it.
        assert exchanged == []

    async def test_a_flow_superseded_during_the_exchange_does_not_land(
        self,
        monkeypatch,
    ):
        # The dangerous interleaving: this flow was current on the way in, and
        # the user started another while its token endpoint kept it waiting.
        # A single pre-flight check passes and the stale grant is stored on top
        # of the newer one.
        from app.api.public import mcp_server as endpoint

        redis = self._redis(current_state="mine")
        stored: list[object] = []
        monkeypatch.setattr(
            endpoint.grant,
            "store_tokens",
            lambda *args, **kwargs: stored.append(args),
        )

        async def slow_exchange(*_args, **_kwargs):
            redis.get.side_effect = self._redis(current_state="newer").get.side_effect
            return MagicMock(access_token="stale")

        monkeypatch.setattr(endpoint.oauth, "exchange_code", slow_exchange)
        monkeypatch.setattr(endpoint, "_load_oauth_client", lambda _c: MagicMock())
        monkeypatch.setattr(
            endpoint.settings,
            "mcp_oauth_redirect_uri",
            "https://app/cb",
        )

        db = self._db()
        with pytest.raises(ConnectorAuthorizationSupersededError) as caught:
            await self._callback(monkeypatch, redis, "mine", db)

        assert caught.value.status_code == 400
        assert stored == []
        # One commit, and it is the read release before the exchange — the grant
        # itself never reached a write. (Asserting *no* commit would pin the
        # connection-holding this endpoint deliberately gave up.)
        assert db.commit.await_count == 1

    async def test_a_grant_is_not_written_onto_a_connector_that_changed_auth(
        self,
        monkeypatch,
    ):
        # The worse of the two: while this flow waited at the token endpoint the
        # user converted the Connector to a bearer token, which clears the OAuth
        # columns. Writing anyway puts a refresh token back on a BEARER row and
        # flips ``connected`` on behind their disconnect.
        from app.api.public import mcp_server as endpoint
        from app.models.mcp_server import MCPServer, MCPServerAuthType

        db = self._db()
        stored: list[object] = []
        monkeypatch.setattr(
            endpoint.grant,
            "store_tokens",
            lambda *args, **kwargs: stored.append(args),
        )
        monkeypatch.setattr(endpoint, "_load_oauth_client", lambda _c: MagicMock())
        monkeypatch.setattr(
            endpoint.settings,
            "mcp_oauth_redirect_uri",
            "https://app/cb",
        )

        async def patched_away(*_args, **_kwargs):
            # The row in the database changes; the session's copy does not.
            db.in_database = MCPServer(
                id=1,
                user_id=7,
                name="c",
                url="https://mcp.example.com/mcp",
                auth_type=MCPServerAuthType.BEARER,
                oauth_client_encrypted=None,
            )
            return MagicMock(access_token="orphan")

        monkeypatch.setattr(endpoint.oauth, "exchange_code", patched_away)

        with pytest.raises(ConnectorChangedDuringAuthorizationError) as caught:
            await self._callback(
                monkeypatch,
                self._redis(current_state="mine"),
                "mine",
                db,
            )

        assert caught.value.status_code == 400
        assert stored == []
        # One commit, and it is the read release before the exchange — the grant
        # itself never reached a write. (Asserting *no* commit would pin the
        # connection-holding this endpoint deliberately gave up.)
        assert db.commit.await_count == 1
        # Locked *and* repopulated — the lock alone leaves the check asking the
        # very object the exchange was holding all along.
        assert db.get.await_args.kwargs["with_for_update"] is True
        assert db.get.await_args.kwargs["populate_existing"] is True

    async def test_a_flow_nothing_is_waiting_for_is_refused(self):
        # An absent pointer used to read as consent, on the theory that it might
        # have expired ahead of its own pending record. It cannot: both are
        # written in one handler with one TTL, the pending record first, so the
        # pending record is always the first to go. What an absent pointer means
        # in practice is that the Connector was disconnected while this flow was
        # in the air — and letting that through undoes the disconnect.
        from app.api.public import mcp_server as endpoint

        with pytest.raises(ConnectorAuthorizationExpiredError) as caught:
            await endpoint._reject_an_unawaited_authorization(
                self._redis(current_state=None),
                "whatever",
                {"connector_id": 1},
            )

        assert caught.value.status_code == 400
        # Told apart from the superseded case, because the remedies differ: there
        # the user finishes the other tab, here they start over.
        assert "no longer waiting" in caught.value.message

    async def test_a_disconnect_mid_flow_is_not_undone_by_the_callback(
        self,
        monkeypatch,
    ):
        # The whole scenario: the popup is open, the user disconnects in another
        # tab, then approves in the popup. Without the pointer being deleted
        # every check passes — the client registration the write is fenced on is
        # deliberately *kept* by a disconnect — and the callback stores a grant
        # and turns ``connected`` back on, so the user's last action loses to
        # their second-to-last.
        from app.api.public import mcp_server as endpoint

        exchanged: list[object] = []

        async def never(*_args, **_kwargs):
            exchanged.append(True)

        monkeypatch.setattr(endpoint.oauth, "exchange_code", never)

        # What a disconnect leaves behind: this flow's pending record is still
        # live for the rest of its TTL, and nothing is pointed at it.
        redis = self._redis(current_state=None)

        with pytest.raises(ConnectorAuthorizationExpiredError) as caught:
            await self._callback(monkeypatch, redis, "the-open-popup")

        assert caught.value.status_code == 400
        # Refused before the code is spent, so there is no grant to discard.
        assert exchanged == []


def _stub_state_consumption(redis: AsyncMock, pending: str) -> None:
    """Make ``redis.pipeline(transaction=True)`` hand back *pending*, once.

    The callback reads and burns the pending authorization in a single
    transaction, so a double that only answers ``get`` would not be exercising
    the code under test at all.
    """
    pipe = AsyncMock()
    pipe.__aenter__.return_value = pipe
    pipe.get = MagicMock()
    pipe.delete = MagicMock()
    pipe.execute = AsyncMock(return_value=[pending, 1])
    redis.pipeline = MagicMock(return_value=pipe)


def _session(**kwargs) -> AsyncMock:
    """An ``AsyncSession`` double whose pending-change sets are empty.

    ``AsyncMock`` invents a truthy attribute on demand, so a bare one claims to
    be holding pending inserts, updates *and* deletes — which is what a session
    that has only read looks nothing like, and what ``_release_connection``
    refuses to commit over. Stating them makes the double honest about being
    fresh.
    """
    db = AsyncMock(**kwargs)
    db.new, db.dirty, db.deleted = set(), set(), set()
    return db


def _db_with_no_connector_named_that() -> AsyncMock:
    """A session whose duplicate-name lookup comes back empty."""
    db = _session()
    db.execute.return_value = MagicMock(first=MagicMock(return_value=None))
    return db


def _sealed(client: OAuthClientInformationFull) -> str:
    from app.services.crypto import encrypt_secret

    return encrypt_secret(client.model_dump_json())


def _dcr_client(client_id: str, *, secret: str = "s") -> OAuthClientInformationFull:
    """A client an authorization server minted — ``client_id_issued_at`` says so."""
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=secret,
        redirect_uris=["https://app/cb"],
        client_id_issued_at=1,
    )


def _manual_client() -> OAuthClientInformationFull:
    """The operator's own OAuth app: a secret, and nothing saying who issued it."""
    return OAuthClientInformationFull(
        client_id="theirs",
        client_secret="theirs",
        redirect_uris=["https://app/cb"],
    )


def _cimd_client() -> OAuthClientInformationFull:
    """Client-id-metadata-document: the id is a public URL, there is no secret."""
    return OAuthClientInformationFull(
        client_id="https://app.example.com/oauth/client",
        redirect_uris=["https://app/cb"],
        token_endpoint_auth_method="none",
    )


class TestAnAuthorizationServerThatChanged:
    """The MCP server names its own authorization server, and can name a
    different one tomorrow. The client on the row belongs to the first."""

    @pytest.fixture(autouse=True)
    def _encryption_key(self, monkeypatch):
        # These tests seal and open real client blobs rather than mocking the
        # store, so the resolver's own decrypt runs.
        from cryptography.fernet import Fernet

        from app.services import crypto

        monkeypatch.setattr(
            crypto.settings,
            "mcp_credential_encryption_key",
            Fernet.generate_key().decode(),
        )
        crypto._cipher.cache_clear()
        yield
        crypto._cipher.cache_clear()

    def _connector(self, issuer: str | None):
        from app.models.mcp_server import MCPServer, MCPServerAuthType

        return MCPServer(
            id=1,
            user_id=7,
            name="c",
            url="https://mcp.example.com/mcp",
            auth_type=MCPServerAuthType.OAUTH,
            oauth_client_encrypted="sealed",
            oauth_metadata=({"authorization_server": {"issuer": issuer}} if issuer else None),
        )

    def _discovered(self, issuer: str):
        return DiscoveredAuthorization(
            resource=ProtectedResourceMetadata(
                resource="https://mcp.example.com/mcp",
                authorization_servers=[issuer],
            ),
            metadata=OAuthMetadata(
                issuer=issuer,
                authorization_endpoint=f"{issuer}/authorize",
                token_endpoint=f"{issuer}/token",
                response_types_supported=["code"],
            ),
        )

    async def _client_for(self, connector, discovered, db=None):
        from app.api.public import mcp_server as endpoint

        return await endpoint._client_for_authorization(
            connector,
            discovered,
            db or AsyncMock(),
        )

    async def test_a_migrated_issuer_re_registers_instead_of_reusing(self, monkeypatch):
        # The DCR secret the *first* server issued must not be posted to
        # whichever one the MCP server points at now — but a client a server
        # minted can be minted again, so the user is not sent away.
        from app.api.public import mcp_server as endpoint

        minted = _dcr_client("fresh")
        monkeypatch.setattr(
            endpoint.oauth,
            "ensure_client",
            AsyncMock(return_value=minted),
        )
        monkeypatch.setattr(
            endpoint.settings,
            "mcp_oauth_redirect_uri",
            "https://app/cb",
        )
        connector = self._connector("https://as-a.example.com/")
        connector.oauth_client_encrypted = _sealed(_dcr_client("old", secret="old"))

        client = await self._client_for(
            connector,
            self._discovered("https://as-evil.example.com"),
        )

        assert client.client_id == "fresh"
        # Recorded, so the next authorization compares against it rather than
        # registering all over again.
        discovered = self._discovered("https://as-evil.example.com")
        assert connector.oauth_metadata["authorization_server"]["issuer"] == str(
            discovered.metadata.issuer
        )
        assert (
            endpoint._why_the_stored_client_is_unusable(
                connector,
                _dcr_client("fresh"),
                discovered,
            )
            is None
        )

    async def test_a_manual_client_is_refused_rather_than_replaced(self, monkeypatch):
        # It is the operator's own OAuth app; there is no second one to make.
        from app.api.public import mcp_server as endpoint

        monkeypatch.setattr(endpoint.oauth, "ensure_client", AsyncMock())
        connector = self._connector("https://as-a.example.com/")
        connector.oauth_client_encrypted = _sealed(_manual_client())

        with pytest.raises(ConnectorOAuthClientUnusableError) as caught:
            await self._client_for(
                connector,
                self._discovered("https://as-evil.example.com"),
            )

        assert caught.value.status_code == 400
        endpoint.oauth.ensure_client.assert_not_awaited()

    async def test_the_same_issuer_reuses_the_stored_client(self):
        connector = self._connector("https://as-a.example.com/")
        connector.oauth_client_encrypted = _sealed(_dcr_client("stored"))

        client = await self._client_for(
            connector,
            self._discovered("https://as-a.example.com"),
        )

        assert client.client_id == "stored"

    async def test_the_authorize_endpoint_actually_checks(self, monkeypatch):
        # Testing the rule in isolation says nothing about whether the endpoint
        # asks it. Drive the real one.
        from app.api.public import mcp_server as endpoint
        from app.rate_limit import limiter

        monkeypatch.setattr(limiter, "enabled", False)
        connector = self._connector("https://as-a.example.com/")
        connector.oauth_client_encrypted = _sealed(_manual_client())

        async def owned(*_args, **_kwargs):
            return connector

        monkeypatch.setattr(endpoint, "_get_owned_connector", owned)
        monkeypatch.setattr(
            endpoint.settings,
            "mcp_oauth_redirect_uri",
            "https://app/cb",
        )
        monkeypatch.setattr(
            endpoint.oauth,
            "discover_authorization",
            AsyncMock(return_value=self._discovered("https://as-evil.example.com")),
        )

        with pytest.raises(ConnectorOAuthClientUnusableError) as caught:
            await endpoint.create_connector_authorization(
                request=MagicMock(),
                connector_id=1,
                db=_session(),
                redis=AsyncMock(),
                current_user=MagicMock(id=7),
            )

        assert caught.value.status_code == 400

    async def test_a_first_authorization_has_nothing_to_compare(self):
        # Nothing registered yet: this really is the first time round.
        connector = self._connector(None)
        connector.oauth_client_encrypted = None

        with pytest.raises(ConnectorOAuthError):
            await self._client_for(connector, self._discovered("https://as-a.example.com"))

    async def test_a_public_client_with_no_recorded_issuer_is_kept(self):
        # Every OAuth connector alive when the issuer column ships has none, so
        # refusing them all would break reconnect for the whole population on
        # deploy day. This one holds no secret — a bare client id offered to the
        # wrong server is rejected there, and nothing of ours travels.
        connector = self._connector(None)
        connector.oauth_client_encrypted = _sealed(_cimd_client())

        client = await self._client_for(
            connector,
            self._discovered("https://as-a.example.com"),
        )

        assert client.client_id == "https://app.example.com/oauth/client"

    async def test_a_confidential_client_with_no_recorded_issuer_is_re_minted(
        self,
        monkeypatch,
    ):
        # It holds a secret whose origin the row cannot prove, so it is not
        # reused — but it says a server issued it, so a new one is registered
        # and the reconnect goes through with the issuer finally recorded.
        from app.api.public import mcp_server as endpoint

        monkeypatch.setattr(
            endpoint.oauth,
            "ensure_client",
            AsyncMock(return_value=_dcr_client("fresh")),
        )
        monkeypatch.setattr(
            endpoint.settings,
            "mcp_oauth_redirect_uri",
            "https://app/cb",
        )
        connector = self._connector(None)
        connector.oauth_client_encrypted = _sealed(_dcr_client("old", secret="old"))

        client = await self._client_for(
            connector,
            self._discovered("https://as-a.example.com"),
        )

        assert client.client_id == "fresh"
        # Recorded, so the next authorization compares against it rather than
        # registering all over again.
        discovered = self._discovered("https://as-a.example.com")
        assert connector.oauth_metadata["authorization_server"]["issuer"] == str(
            discovered.metadata.issuer
        )
        assert (
            endpoint._why_the_stored_client_is_unusable(
                connector,
                _dcr_client("fresh"),
                discovered,
            )
            is None
        )

    async def test_an_unprovable_confidential_client_is_refused(self, monkeypatch):
        # No issuer recorded, a secret, and nothing saying a server minted it:
        # it may be the operator's own app, so it is neither used nor replaced.
        from app.api.public import mcp_server as endpoint

        monkeypatch.setattr(endpoint.oauth, "ensure_client", AsyncMock())
        connector = self._connector(None)
        connector.oauth_client_encrypted = _sealed(_manual_client())

        with pytest.raises(ConnectorOAuthClientUnusableError) as caught:
            await self._client_for(
                connector,
                self._discovered("https://as-a.example.com"),
            )

        assert caught.value.status_code == 400
        endpoint.oauth.ensure_client.assert_not_awaited()


class TestAnExpiredClientRegistration:
    """A DCR client can be given an expiry of its own. Past it the server says
    ``invalid_client``, which sends the user to Reconnect — and Reconnect must
    not hand them the same dead registration."""

    def test_a_stated_expiry_in_the_past_counts(self):
        from app.api.public.mcp_server import _secret_expired

        assert _secret_expired(
            OAuthClientInformationFull(
                client_id="cid",
                redirect_uris=["https://app/cb"],
                client_secret="s",
                client_secret_expires_at=1,
            )
        )

    def test_zero_means_never(self):
        # RFC 7591 §3.2.1. Reading it as a 1970 deadline would re-register a
        # perfectly good client on every authorization.
        from app.api.public.mcp_server import _secret_expired

        assert not _secret_expired(
            OAuthClientInformationFull(
                client_id="cid",
                redirect_uris=["https://app/cb"],
                client_secret="s",
                client_secret_expires_at=0,
            )
        )

    def test_a_manual_client_is_never_re_registered(self):
        # It belongs to the operator's own OAuth app and carries no expiry, so
        # it must not be replaced behind their back.
        from app.api.public.mcp_server import _secret_expired

        assert not _secret_expired(
            OAuthClientInformationFull(
                client_id="theirs",
                redirect_uris=["https://app/cb"],
                client_secret="theirs",
            )
        )


# The document ``mcp_server.oauth_metadata`` holds, as a provider that supports
# revocation publishes it.
_AUTHORIZATION_SERVER = {
    "issuer": "https://as.example.com",
    "authorization_endpoint": "https://as.example.com/authorize",
    "token_endpoint": "https://as.example.com/token",
    "revocation_endpoint": "https://as.example.com/revoke",
    "response_types_supported": ["code"],
}


def _stored_connector(**over):
    """An OAuth Connector as a row that has been through authorization.

    Every server-default column is set explicitly. A transient ORM object never
    materialises a default, so an unset one reads as ``None`` — which the
    response model rejects, and which makes a clear that missed a column look
    like a clear that worked.
    """
    from app.models.mcp_server import MCPServer, MCPServerAuthType

    base = dict(
        id=1,
        user_id=7,
        name="c",
        url="https://mcp.example.com/mcp",
        auth_type=MCPServerAuthType.OAUTH,
        oauth_client_encrypted="sealed-client",
        oauth_tokens_encrypted="sealed-tokens",
        oauth_scope="files:read",
        oauth_metadata={"authorization_server": _AUTHORIZATION_SERVER},
        oauth_tokens_expires_at=_NOW,
        disabled_tools=[],
        advertised_icons=[],
        tool_count=0,
        connected=True,
        oauth_reauthorization_required=False,
        last_connect_failed=False,
        created_at=_NOW,
        updated_at=_NOW,
    )
    base.update(over)
    return MCPServer(**base)


async def _patch_connector(
    monkeypatch,
    connector,
    body,
    *,
    redis=None,
) -> BackgroundTasks:
    """Run ``update_connector`` against *connector*, with only I/O stubbed.

    Returns the background tasks it queued. Not the response body: every caller
    asserts on the row it handed in, which is the thing under test — the
    serialized copy would only be a second spelling of it. Pass *redis* when the
    assertion is about what the request did to the pending-authorization keys.
    """
    from app.api.public import mcp_server as endpoint
    from app.rate_limit import limiter
    from app.services.mcp.probe import ProbeResult

    # The endpoint is rate-limit decorated, which insists on a real request.
    monkeypatch.setattr(limiter, "enabled", False)
    # The URL guard has its own cover in test_connector_url_uniqueness; here it
    # would only answer from a double that knows nothing.
    monkeypatch.setattr(endpoint, "_reject_duplicate_url", AsyncMock())

    async def owned(*_args, **_kwargs):
        return connector

    monkeypatch.setattr(endpoint, "_get_owned_connector", owned)
    monkeypatch.setattr(endpoint, "_commit_or_conflict", AsyncMock())
    monkeypatch.setattr(
        endpoint,
        "probe",
        AsyncMock(return_value=ProbeResult(server_name="s", tools=[])),
    )
    # Every PATCH reads the static credential back, whether or not it is
    # changing one. Nothing here is about crypto, so the seal is a no-op.
    monkeypatch.setattr(endpoint, "open_secret", lambda sealed, **_: sealed)

    background_tasks = BackgroundTasks()
    await endpoint.update_connector(
        request=MagicMock(),
        connector_id=1,
        body=body,
        background_tasks=background_tasks,
        db=_session(),
        redis=redis or AsyncMock(),
        current_user=MagicMock(id=7),
    )
    return background_tasks


class TestRepointingAnOAuthConnector:
    """An OAuth grant is issued for one resource; the URL cannot move under it."""

    async def test_the_url_cannot_be_changed(self, monkeypatch):
        # Dropping the grant is mandatory — it is bound to the old resource —
        # and the client registration has to go with it, since the old
        # authorization server issued it and may have given it a secret. That
        # leaves a row nothing can re-provision: the authorize endpoint needs a
        # client registration and a PATCH cannot make one. So the change is
        # refused rather than performed into a dead end.
        from app.schemas.mcp_server import ConnectorUpdate

        connector = _stored_connector(url="https://old.example.com/mcp")

        with pytest.raises(ConnectorOAuthChangeUnsupportedError) as caught:
            await _patch_connector(
                monkeypatch,
                connector,
                ConnectorUpdate(url="https://new.example.com/mcp"),
            )

        assert caught.value.status_code == 400
        assert connector.url == "https://old.example.com/mcp"
        assert connector.oauth_client_encrypted == "sealed-client"
        assert connector.oauth_tokens_encrypted == "sealed-tokens"

    async def test_everything_else_about_it_still_changes(self, monkeypatch):
        # The refusal is about the URL alone; renaming or switching tools off
        # must keep working on an authorized Connector.
        from app.schemas.mcp_server import ConnectorUpdate

        connector = _stored_connector(url="https://old.example.com/mcp")

        await _patch_connector(
            monkeypatch,
            connector,
            ConnectorUpdate(name="renamed", disabled_tools=["delete_page"]),
        )

        assert (connector.name, connector.disabled_tools) == (
            "renamed",
            ["delete_page"],
        )
        assert connector.oauth_tokens_encrypted == "sealed-tokens"

    async def test_a_static_credential_connector_still_moves(self, monkeypatch):
        # The rule is about the grant, not about the column.
        from app.models.mcp_server import MCPServerAuthType
        from app.schemas.mcp_server import ConnectorUpdate

        connector = _stored_connector(url="https://old.example.com/mcp")
        connector.auth_type = MCPServerAuthType.NONE
        connector.oauth_client_encrypted = None

        await _patch_connector(
            monkeypatch,
            connector,
            ConnectorUpdate(url="https://new.example.com/mcp"),
        )

        assert connector.url == "https://new.example.com/mcp"


class TestSwitchingAwayFromOAuth:
    """Converting a Connector to a static credential must leave nothing an
    OAuth code path can still act on."""

    async def test_every_oauth_column_is_cleared(self, monkeypatch):
        # The stored metadata is the one that bites: left behind, the token
        # refresher still knows where to aim a rotation and would write an OAuth
        # token back onto a row the user just converted to a static credential.
        #
        # Driven through the endpoint rather than the helper that does the
        # clearing. Two helpers share the job — one owns the authorization, the
        # other the standing to ask for one — so a test naming either of them
        # would pass while the conversion path called only that one.
        from app.api.public import mcp_server as endpoint
        from app.models.mcp_server import MCPServer, MCPServerAuthType
        from app.schemas.mcp_server import ConnectorUpdate

        # Every ``oauth_*`` column gets a truthy value first. A fixture that
        # leaves one unset cannot catch a clear that misses it — on a transient
        # ORM object the server default has not materialised, so the column
        # reads as ``None`` and looks cleared either way.
        oauth_columns = [
            column for column in MCPServer.__table__.columns.keys() if column.startswith("oauth_")
        ]
        connector = _stored_connector()
        marks = {
            "oauth_tokens_expires_at": _NOW,
            "oauth_metadata": {"authorization_server": _AUTHORIZATION_SERVER},
            "oauth_reauthorization_required": True,
        }
        for column in oauth_columns:
            setattr(connector, column, marks.get(column, "set"))
        monkeypatch.setattr(endpoint, "encrypt_secret", lambda plain: plain)

        await _patch_connector(
            monkeypatch,
            connector,
            ConnectorUpdate(auth_type=MCPServerAuthType.BEARER, credential="tok"),
        )

        left_over = [column for column in oauth_columns if getattr(connector, column)]
        assert left_over == []


class TestEndingAnAuthorization:
    """Three things end one — disconnecting, converting to a static credential,
    and deleting — and all three mean what the user takes them to mean: we stop
    holding an authorization, and the provider stops holding one for us. Keeping
    the tokens and merely hiding the tools would leave a live refresh token
    behind something the user believes they have already ended."""

    def _stub_grant(self, monkeypatch, *, refresh_token="R0"):
        """The row's sealed blobs, as their decrypted selves."""
        from mcp.shared.auth import OAuthToken

        from app.api.public import mcp_server as endpoint

        tokens = OAuthToken(
            access_token="AT",
            token_type="Bearer",
            refresh_token=refresh_token,
        )
        monkeypatch.setattr(
            endpoint.grant,
            "stored_tokens",
            lambda _row: tokens,
        )
        monkeypatch.setattr(
            endpoint.grant,
            "open_secret",
            lambda _c, **_: _client().model_dump_json(),
        )
        return tokens

    async def test_the_grant_goes_and_the_registration_stays(self, monkeypatch):
        # The split that makes reconnecting one browser round-trip rather than
        # a fresh dynamic registration against a provider that already knows us.
        from app.schemas.mcp_server import ConnectorUpdate

        self._stub_grant(monkeypatch)
        connector = _stored_connector()

        await _patch_connector(
            monkeypatch,
            connector,
            ConnectorUpdate(connected=False),
        )

        assert connector.connected is False
        assert connector.oauth_tokens_encrypted is None
        assert connector.oauth_tokens_expires_at is None
        assert connector.oauth_client_encrypted == "sealed-client"
        assert connector.oauth_metadata is not None

    async def test_the_provider_is_told_after_the_response(self, monkeypatch):
        # Queued rather than awaited: our copy is gone either way, so making
        # the user wait would turn "disconnect" into a spinner over someone
        # else's outage.
        from app.api.public import mcp_server as endpoint
        from app.schemas.mcp_server import ConnectorUpdate

        tokens = self._stub_grant(monkeypatch)

        queued = await _patch_connector(
            monkeypatch,
            _stored_connector(),
            ConnectorUpdate(connected=False),
        )

        assert [task.func for task in queued.tasks] == [endpoint._revoke_quietly]
        assert queued.tasks[0].args[-1] is tokens

    async def test_reconnecting_without_a_grant_is_refused(self, monkeypatch):
        # ``connected=True`` cannot restore what disconnecting gave up, and a row
        # connected without a grant serves no call: the worker sends no bearer,
        # the server answers 401, and the turn writes the Connector down as
        # unreachable — a diagnosis pointing at the server rather than at the
        # missing authorization. The UI sends the user to the provider instead;
        # this is the endpoint saying so rather than trusting it to.
        from app.schemas.mcp_server import ConnectorUpdate

        self._stub_grant(monkeypatch)
        connector = _stored_connector(
            connected=False,
            oauth_tokens_encrypted=None,
            oauth_tokens_expires_at=None,
        )

        with pytest.raises(ConnectorNotAuthorizedError) as caught:
            await _patch_connector(
                monkeypatch,
                connector,
                ConnectorUpdate(connected=True),
            )

        assert caught.value.status_code == 400
        assert connector.connected is False

    async def test_a_row_that_still_has_its_grant_reconnects_with_one_flag(
        self,
        monkeypatch,
    ):
        # Rows disconnected before disconnecting began giving the grant up. They
        # hold a usable one, so the flag is the whole job, and the refusal above
        # must not catch them.
        from app.schemas.mcp_server import ConnectorUpdate

        self._stub_grant(monkeypatch)
        connector = _stored_connector(connected=False)

        queued = await _patch_connector(
            monkeypatch,
            connector,
            ConnectorUpdate(connected=True),
        )

        assert connector.connected is True
        assert connector.oauth_tokens_encrypted == "sealed-tokens"
        assert queued.tasks == []

    async def test_a_static_credential_connector_connects_freely(
        self,
        monkeypatch,
    ):
        # The refusal is about a grant, so it must not reach the auth types that
        # never have one — their credential is on the row and always was.
        from app.models.mcp_server import MCPServerAuthType
        from app.schemas.mcp_server import ConnectorUpdate

        connector = _stored_connector(
            auth_type=MCPServerAuthType.NONE,
            connected=False,
            oauth_client_encrypted=None,
            oauth_tokens_encrypted=None,
            oauth_scope=None,
            oauth_metadata=None,
            oauth_tokens_expires_at=None,
        )

        await _patch_connector(
            monkeypatch,
            connector,
            ConnectorUpdate(connected=True),
        )

        assert connector.connected is True

    async def test_a_credential_the_user_typed_is_not_thrown_away(
        self,
        monkeypatch,
    ):
        # The rule is "give back what the provider delegated to us", not "clear
        # the row". A bearer token is the user's own secret, and discarding it
        # would make disconnecting cost them a trip to find it again.
        from app.models.mcp_server import MCPServerAuthType
        from app.schemas.mcp_server import ConnectorUpdate

        connector = _stored_connector(
            auth_type=MCPServerAuthType.BEARER,
            encrypted_credential="sealed-bearer",
            oauth_client_encrypted=None,
            oauth_tokens_encrypted=None,
            oauth_scope=None,
            oauth_metadata=None,
            oauth_tokens_expires_at=None,
        )

        queued = await _patch_connector(
            monkeypatch,
            connector,
            ConnectorUpdate(connected=False),
        )

        assert connector.encrypted_credential == "sealed-bearer"
        assert queued.tasks == []

    async def test_a_provider_offering_no_revocation_is_not_guessed_at(
        self,
        monkeypatch,
    ):
        # Nothing is sent anywhere: an endpoint that was never advertised for a
        # refresh token is not a place to post one. Our copy still goes.
        from app.schemas.mcp_server import ConnectorUpdate

        self._stub_grant(monkeypatch)
        without_revocation = {
            key: value
            for key, value in _AUTHORIZATION_SERVER.items()
            if key != "revocation_endpoint"
        }
        connector = _stored_connector(
            oauth_metadata={"authorization_server": without_revocation},
        )

        queued = await _patch_connector(
            monkeypatch,
            connector,
            ConnectorUpdate(connected=False),
        )

        assert connector.oauth_tokens_encrypted is None
        assert queued.tasks == []

    async def test_deleting_tells_the_provider_too(self, monkeypatch):
        # The same orphan, reached the other way: the row goes but the
        # authorization would stay listed on the user's account.
        from app.api.public import mcp_server as endpoint

        self._stub_grant(monkeypatch)

        async def owned(*_args, **_kwargs):
            return _stored_connector()

        monkeypatch.setattr(endpoint, "_get_owned_connector", owned)
        background_tasks = BackgroundTasks()

        await endpoint.delete_connector(
            connector_id=1,
            background_tasks=background_tasks,
            db=_session(),
            redis=AsyncMock(),
            current_user=MagicMock(id=7),
        )

        assert [task.func for task in background_tasks.tasks] == [endpoint._revoke_quietly]

    async def test_a_provider_that_refuses_the_revocation_is_only_logged(
        self,
        monkeypatch,
    ):
        # It runs after the response, so there is nobody left to tell — and
        # Starlette re-raises what a background task raises, which would log a
        # request-shaped traceback for a request that already succeeded.
        from app.api.public import mcp_server as endpoint

        monkeypatch.setattr(
            endpoint.oauth,
            "revoke_grant",
            AsyncMock(side_effect=ConnectorOAuthError("nope")),
        )

        await endpoint._revoke_quietly(1, MagicMock(), MagicMock(), MagicMock())

    async def test_converting_to_a_static_credential_tells_the_provider_too(
        self,
        monkeypatch,
    ):
        # The third way a user ends an authorization. The grant is dropped here
        # whatever happens — it is unreachable once the row is no longer OAuth —
        # so the provider has to hear about it on this path as well.
        from app.api.public import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType
        from app.schemas.mcp_server import ConnectorUpdate
        from app.services.mcp.probe import ProbeResult

        self._stub_grant(monkeypatch)
        monkeypatch.setattr(endpoint, "encrypt_secret", lambda plain: plain)
        monkeypatch.setattr(
            endpoint,
            "probe",
            AsyncMock(return_value=ProbeResult(server_name="s", tools=[])),
        )
        connector = _stored_connector()

        queued = await _patch_connector(
            monkeypatch,
            connector,
            ConnectorUpdate(auth_type=MCPServerAuthType.BEARER, credential="tok"),
        )

        assert connector.oauth_tokens_encrypted is None
        assert connector.oauth_client_encrypted is None
        assert [task.func for task in queued.tasks] == [endpoint._revoke_quietly]

    async def test_a_grant_that_cannot_be_read_still_lets_go(self, monkeypatch):
        # Sealed under an encryption key that has since been rotated. Reading it
        # is the first thing on this path to need that key, and failing here
        # would leave the user unable to disconnect *or* delete the one kind of
        # Connector they most need to be rid of.
        from app.api.public import mcp_server as endpoint
        from app.schemas.mcp_server import ConnectorUpdate

        def unreadable(_ciphertext, **_):
            raise ConnectorCredentialUnreadableError("sealed under a rotated key")

        monkeypatch.setattr(endpoint.grant, "open_secret", unreadable)
        connector = _stored_connector()

        queued = await _patch_connector(
            monkeypatch,
            connector,
            ConnectorUpdate(connected=False),
        )

        assert connector.connected is False
        assert connector.oauth_tokens_encrypted is None
        assert queued.tasks == []

    async def test_deleting_one_that_cannot_be_read_still_works(self, monkeypatch):
        from app.api.public import mcp_server as endpoint

        def unreadable(_ciphertext, **_):
            raise ConnectorCredentialUnreadableError("sealed under a rotated key")

        monkeypatch.setattr(endpoint.grant, "open_secret", unreadable)

        async def owned(*_args, **_kwargs):
            return _stored_connector()

        monkeypatch.setattr(endpoint, "_get_owned_connector", owned)
        db = _session()

        await endpoint.delete_connector(
            connector_id=1,
            background_tasks=BackgroundTasks(),
            db=db,
            redis=AsyncMock(),
            current_user=MagicMock(id=7),
        )

        db.delete.assert_awaited_once()

    async def test_an_authorization_in_the_air_is_given_up_too(self, monkeypatch):
        # The other half of the scenario above, from this side. A ``state`` the
        # user started before disconnecting stays valid for its whole TTL, so the
        # pointer naming it has to go — that is what makes the callback refuse.
        # The pointer and not the pending records: it is the only key this
        # request can name, and deleting it retires every flow at once rather
        # than the newest of them.
        from app.schemas.mcp_server import ConnectorUpdate
        from app.services.redis import oauth_current_state_key

        self._stub_grant(monkeypatch)
        redis = AsyncMock()

        await _patch_connector(
            monkeypatch,
            _stored_connector(),
            ConnectorUpdate(connected=False),
            redis=redis,
        )

        redis.delete.assert_awaited_once_with(oauth_current_state_key(1))

    async def test_converting_also_retires_a_flow_in_the_air(self, monkeypatch):
        # What the split calls used to miss. A conversion was covered only by
        # accident — the callback fences the write on ``oauth_client_encrypted``,
        # which a conversion clears — so the flow was refused for a reason that
        # had nothing to do with the user's intent, and would stop being refused
        # the day that column stopped being cleared.
        from app.api.public import mcp_server as endpoint
        from app.models.mcp_server import MCPServerAuthType
        from app.schemas.mcp_server import ConnectorUpdate
        from app.services.redis import oauth_current_state_key

        self._stub_grant(monkeypatch)
        monkeypatch.setattr(endpoint, "encrypt_secret", lambda plain: plain)
        redis = AsyncMock()

        await _patch_connector(
            monkeypatch,
            _stored_connector(),
            ConnectorUpdate(auth_type=MCPServerAuthType.BEARER, credential="tok"),
            redis=redis,
        )

        redis.delete.assert_awaited_once_with(oauth_current_state_key(1))

    async def test_deleting_also_retires_a_flow_in_the_air(self, monkeypatch):
        # Likewise: covered only by the callback failing to find the row. Leaving
        # the pointer behind also leaves a Redis key naming a Connector that no
        # longer exists, for the rest of its TTL.
        from app.api.public import mcp_server as endpoint
        from app.services.redis import oauth_current_state_key

        self._stub_grant(monkeypatch)

        async def owned(*_args, **_kwargs):
            return _stored_connector()

        monkeypatch.setattr(endpoint, "_get_owned_connector", owned)
        redis = AsyncMock()

        await endpoint.delete_connector(
            connector_id=1,
            background_tasks=BackgroundTasks(),
            db=_session(),
            redis=redis,
            current_user=MagicMock(id=7),
        )

        redis.delete.assert_awaited_once_with(oauth_current_state_key(1))

    async def test_redis_being_down_leaves_the_ending_untaken(self, monkeypatch):
        # Not "it fails" but *how* it fails: nothing must be half-done. Wrapping
        # the Redis call in a swallow — the tempting defensive edit — would commit
        # a disconnect while leaving the flow in the air live, which is the one
        # outcome this whole path exists to prevent. So the property is pinned
        # rather than left to whoever reads the docstring.
        from app.api.public import mcp_server as endpoint
        from app.rate_limit import limiter
        from app.schemas.mcp_server import ConnectorUpdate

        monkeypatch.setattr(limiter, "enabled", False)
        self._stub_grant(monkeypatch)
        connector = _stored_connector()

        async def owned(*_args, **_kwargs):
            return connector

        monkeypatch.setattr(endpoint, "_get_owned_connector", owned)
        monkeypatch.setattr(endpoint, "open_secret", lambda sealed, **_: sealed)
        commit = AsyncMock()
        monkeypatch.setattr(endpoint, "_commit_or_conflict", commit)
        redis = AsyncMock()
        redis.delete.side_effect = ConnectionError("redis is down")

        with pytest.raises(ConnectionError):
            await endpoint.update_connector(
                request=MagicMock(),
                connector_id=1,
                body=ConnectorUpdate(connected=False),
                background_tasks=BackgroundTasks(),
                db=_session(),
                redis=redis,
                current_user=MagicMock(id=7),
            )

        # Nothing committed, so the row still holds its grant and is still
        # connected; the queued revocation never reaches a response to run from.
        commit.assert_not_awaited()
