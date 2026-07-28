"""Probe a remote MCP server: auth headers, success mapping, failure paths."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.errors import ConnectorUnreachableError, InvalidConnectorUrlError
from app.models.mcp_server import MCPServerAuthType
from app.services.mcp import probe as probe_mod
from app.services.mcp.probe import build_auth_headers, probe


class TestBuildAuthHeaders:
    def test_none(self):
        assert build_auth_headers(MCPServerAuthType.NONE, None) == {}

    def test_bearer(self):
        assert build_auth_headers(MCPServerAuthType.BEARER, "tok") == {
            "Authorization": "Bearer tok",
        }

    def test_header(self):
        creds = '{"header": "X-Api-Key", "value": "k"}'
        assert build_auth_headers(MCPServerAuthType.HEADER, creds) == {"X-Api-Key": "k"}


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        # ``icons`` mirrors the real ``Implementation``; leaving it off would
        # only prove the code tolerates a fake that can't occur.
        return SimpleNamespace(
            serverInfo=SimpleNamespace(name="Demo Server", icons=None)
        )

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name="search", description="Search things"),
            SimpleNamespace(name="create", description=None),
        ])


class TestProbe:
    async def test_success_maps_name_and_tools(self, monkeypatch):
        async def _guard_ok(url):
            return None

        monkeypatch.setattr(probe_mod, "guard_public_url", _guard_ok)

        @asynccontextmanager
        async def fake_shc(*args, **kwargs):
            yield ("r", "w", lambda: None)

        monkeypatch.setattr(probe_mod, "streamablehttp_client", fake_shc)
        monkeypatch.setattr(
            probe_mod, "ClientSession", lambda read, write: _FakeSession()
        )

        result = await probe("https://mcp.example.com/x", MCPServerAuthType.NONE, None)

        assert result.server_name == "Demo Server"
        assert result.tools == [
            {"name": "search", "description": "Search things"},
            {"name": "create", "description": ""},
        ]

    async def test_ssrf_failure_propagates_not_wrapped(self, monkeypatch):
        # A bad URL must surface as InvalidConnectorUrlError, not be collapsed
        # into ConnectorUnreachableError.
        with pytest.raises(InvalidConnectorUrlError):
            await probe("https://10.0.0.1/x", MCPServerAuthType.NONE, None)

    async def test_handshake_failure_becomes_unreachable(self, monkeypatch):
        async def _guard_ok(url):
            return None

        monkeypatch.setattr(probe_mod, "guard_public_url", _guard_ok)

        @asynccontextmanager
        async def boom(*args, **kwargs):
            raise RuntimeError("connection refused")
            yield  # pragma: no cover

        monkeypatch.setattr(probe_mod, "streamablehttp_client", boom)
        with pytest.raises(ConnectorUnreachableError):
            await probe("https://mcp.example.com/x", MCPServerAuthType.NONE, None)


class TestServerIcon:
    """``serverInfo.icons`` (SEP-973) gives a connector its own artwork; only
    sources the browser can load directly are kept."""

    def _server_info(self, *srcs):
        from types import SimpleNamespace

        return SimpleNamespace(
            icons=[SimpleNamespace(src=src) for src in srcs]
        )

    def test_takes_an_https_icon(self):
        from app.services.mcp.probe import _usable_icon

        assert _usable_icon(self._server_info("https://cdn.example.com/i.png")) == (
            "https://cdn.example.com/i.png"
        )

    def test_takes_an_inline_image_data_uri(self):
        from app.services.mcp.probe import _usable_icon

        src = "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="
        assert _usable_icon(self._server_info(src)) == src

    def test_skips_sources_the_browser_should_not_load(self):
        from app.services.mcp.probe import _usable_icon

        for bad in (
            "http://cdn.example.com/i.png",       # plaintext
            "javascript:alert(1)",                 # not an image at all
            "data:text/html,<script>x</script>",   # data URI, wrong type
            "file:///etc/passwd",
        ):
            assert _usable_icon(self._server_info(bad)) is None

    def test_skips_an_oversize_payload(self):
        from app.services.mcp.probe import _MAX_ICON_SRC_LEN, _usable_icon

        assert _usable_icon(
            self._server_info("data:image/png;base64," + "A" * _MAX_ICON_SRC_LEN)
        ) is None

    def test_falls_through_to_the_first_usable_one(self):
        from app.services.mcp.probe import _usable_icon

        assert _usable_icon(
            self._server_info("file:///x", "https://ok.example.com/i.png")
        ) == "https://ok.example.com/i.png"

    def test_no_icons_is_not_an_error(self):
        from types import SimpleNamespace

        from app.services.mcp.probe import _usable_icon

        assert _usable_icon(SimpleNamespace(icons=None)) is None
        assert _usable_icon(None) is None
