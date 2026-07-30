"""SSRF guard for user-supplied Connector URLs."""

import httpx
import pytest

from app.errors import InvalidConnectorUrlError
from app.services.mcp import url_guard


class TestValidatePublicUrl:
    @pytest.mark.parametrize("url", [
        "http://mcp.example.com/x",    # not https
        "https:///x",                  # no host
        "https://127.0.0.1/x",         # loopback
        "https://10.0.0.5/x",          # private
        "https://192.168.1.1/x",       # private
        "https://169.254.169.254/x",   # cloud metadata / link-local
        # Shared address space (RFC 6598). Neither ``is_private`` nor
        # ``is_loopback``, so a blocklist waves it through — and it is where
        # Tailscale, carrier-grade NAT and plenty of internal services live.
        "https://100.64.0.10/x",
        "https://192.0.0.1/x",         # IETF protocol assignments
        "https://198.18.0.1/x",        # benchmarking range
        "https://[fc00::1]/x",         # IPv6 unique-local
    ])
    def test_rejects_unsafe(self, url):
        with pytest.raises(InvalidConnectorUrlError):
            url_guard.validate_public_url(url)

    def test_accepts_public(self, monkeypatch):
        monkeypatch.setattr(
            url_guard.socket, "getaddrinfo",
            lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        url_guard.validate_public_url("https://mcp.example.com/x")  # no raise

    def test_unresolvable_host_rejected(self, monkeypatch):
        import socket

        def boom(*args, **kwargs):
            raise socket.gaierror("nope")

        monkeypatch.setattr(url_guard.socket, "getaddrinfo", boom)
        with pytest.raises(InvalidConnectorUrlError):
            url_guard.validate_public_url("https://no.such.host.invalid/x")


class TestPinnedResolveTransport:
    async def test_rejects_rebind_to_private_ip(self, monkeypatch):
        # The DNS-pinning transport re-resolves at connect time and rejects a
        # host that resolves to a private IP — closing the rebinding TOCTOU a
        # pre-flight check alone leaves open. (loop.getaddrinfo delegates to
        # socket.getaddrinfo, so patching it drives the async resolution.)
        monkeypatch.setattr(
            url_guard.socket, "getaddrinfo",
            lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.5", 443))],
        )
        transport = url_guard._PinnedResolveTransport()
        with pytest.raises(InvalidConnectorUrlError):
            await transport.handle_async_request(
                httpx.Request("GET", "https://rebind.example.com/")
            )

    async def test_falls_over_to_the_next_address(self, monkeypatch):
        # A CDN name answers with several addresses and ``getaddrinfo`` rotates
        # their order per call, so pinning the first one alone failed requests at
        # random — one unreachable edge, or an IPv6 record on a host with no IPv6
        # route, and the next request would succeed where this one did not. An
        # ordinary client tries each; pinning must not cost that.
        monkeypatch.setattr(
            url_guard.socket, "getaddrinfo",
            lambda *args, **kwargs: [
                (10, 1, 6, "", ("2602:f79a::1", 443, 0, 0)),
                (2, 1, 6, "", ("208.103.161.1", 443)),
            ],
        )
        attempted: list[str] = []

        async def attempt(self, request, **kwargs):
            attempted.append(request.url.host)
            if ":" in request.url.host:
                raise httpx.ConnectError("Network is unreachable")
            return httpx.Response(200)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", attempt)
        transport = url_guard._PinnedResolveTransport()

        response = await transport.handle_async_request(
            httpx.Request("GET", "https://cdn.example.com/")
        )

        assert response.status_code == 200
        assert attempted == ["2602:f79a::1", "208.103.161.1"]

    async def test_a_read_failure_is_not_retried(self, monkeypatch):
        # It happens after the request was sent, so the server may already have
        # acted on it — re-sending a token exchange would spend the grant twice.
        monkeypatch.setattr(
            url_guard.socket, "getaddrinfo",
            lambda *args, **kwargs: [
                (2, 1, 6, "", ("208.103.161.1", 443)),
                (2, 1, 6, "", ("208.103.161.2", 443)),
            ],
        )
        attempts = 0

        async def attempt(self, request, **kwargs):
            nonlocal attempts
            attempts += 1
            raise httpx.ReadError("connection reset mid-response")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", attempt)
        transport = url_guard._PinnedResolveTransport()

        with pytest.raises(httpx.ReadError):
            await transport.handle_async_request(
                httpx.Request("POST", "https://cdn.example.com/token")
            )

        assert attempts == 1


class TestGuardedClient:
    def test_does_not_follow_redirects(self):
        assert url_guard.guarded_client().follow_redirects is False


class TestSchemeEnforcedPerRequest:
    """A server's advertised token / registration endpoints never pass through
    the pre-flight check, so the transport itself has to refuse plaintext."""

    async def test_a_plaintext_metadata_endpoint_is_refused(self, monkeypatch):
        # The pre-flight guard only ever saw the MCP server URL; this is the
        # http:// token endpoint that URL's metadata pointed at.
        from app.errors import InvalidConnectorUrlError
        from app.services.mcp.url_guard import guarded_client

        async with guarded_client() as client:
            with pytest.raises(InvalidConnectorUrlError, match="non-https"):
                await client.post("http://public.example/oauth/token", data={})

    async def test_https_still_reaches_dns_pinning(self, monkeypatch):
        # Scheme passes, so the request proceeds to the address check — which
        # is what rejects it here.
        from app.errors import InvalidConnectorUrlError
        from app.services.mcp import url_guard

        monkeypatch.setattr(
            url_guard.asyncio.get_running_loop().__class__, "getaddrinfo",
            _resolving_to("10.0.0.5"), raising=False,
        )
        async with url_guard.guarded_client() as client:
            with pytest.raises(InvalidConnectorUrlError):
                await client.post("https://internal.example/oauth/token", data={})


def _resolving_to(address: str):
    async def _getaddrinfo(self, host, port, **kwargs):
        return [(2, 1, 6, "", (address, port))]
    return _getaddrinfo
