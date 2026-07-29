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
