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
        return SimpleNamespace(serverInfo=SimpleNamespace(name="Demo Server"))

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
        async def fake_shc(*a, **k):
            yield ("r", "w", lambda: None)

        monkeypatch.setattr(probe_mod, "streamablehttp_client", fake_shc)
        monkeypatch.setattr(probe_mod, "ClientSession", lambda r, w: _FakeSession())

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
        async def boom(*a, **k):
            raise RuntimeError("connection refused")
            yield  # pragma: no cover

        monkeypatch.setattr(probe_mod, "streamablehttp_client", boom)
        with pytest.raises(ConnectorUnreachableError):
            await probe("https://mcp.example.com/x", MCPServerAuthType.NONE, None)
