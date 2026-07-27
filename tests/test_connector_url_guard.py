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
    ])
    def test_rejects_unsafe(self, url):
        with pytest.raises(InvalidConnectorUrlError):
            url_guard.validate_public_url(url)

    def test_accepts_public(self, monkeypatch):
        monkeypatch.setattr(
            url_guard.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        url_guard.validate_public_url("https://mcp.example.com/x")  # no raise

    def test_unresolvable_host_rejected(self, monkeypatch):
        import socket

        def boom(*a, **k):
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
            lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443))],
        )
        transport = url_guard._PinnedResolveTransport()
        with pytest.raises(InvalidConnectorUrlError):
            await transport.handle_async_request(
                httpx.Request("GET", "https://rebind.example.com/")
            )


class TestGuardedClient:
    def test_does_not_follow_redirects(self):
        assert url_guard.guarded_client().follow_redirects is False
