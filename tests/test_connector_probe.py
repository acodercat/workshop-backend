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

    async def list_tools(self, cursor=None):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name="search", description="Search things"),
                SimpleNamespace(name="create", description=None),
            ],
            nextCursor=None,
        )


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


class TestServerIcons:
    """``serverInfo.icons`` (SEP-973) gives a connector its own artwork. Every
    source the browser can load directly is kept, because which one is best
    depends on the device and the colour scheme — and only the browser knows
    those."""

    def _server_info(self, *icons):
        """A ``serverInfo`` whose icons are the given dicts, SDK-shaped."""
        from mcp.types import Icon, Implementation

        return Implementation(
            name="s",
            version="1",
            icons=[Icon.model_validate(icon) for icon in icons],
        )

    def _srcs(self, server_info):
        from app.services.mcp.probe import _usable_icons

        return [icon["src"] for icon in _usable_icons(server_info)]

    def test_keeps_every_usable_candidate_in_order(self):
        # The point of the change: the browser needs the whole set to choose
        # from, so a first-usable scan threw away what the choice depends on.
        assert self._srcs(
            self._server_info(
                {"src": "https://cdn.example.com/48.png", "sizes": ["48x48"]},
                {"src": "https://cdn.example.com/96.png", "sizes": ["96x96"]},
            )
        ) == ["https://cdn.example.com/48.png", "https://cdn.example.com/96.png"]

    def test_carries_the_fields_the_browser_chooses_by(self):
        from app.services.mcp.probe import _usable_icons

        [icon] = _usable_icons(
            self._server_info({
                "src": "https://cdn.example.com/i.png",
                "mimeType": "image/png",
                "sizes": ["48x48"],
                # Not modelled by the SDK, so it arrives as an extra field —
                # dropping it would take dark-mode support with it.
                "theme": "dark",
            })
        )

        assert icon == {
            "src": "https://cdn.example.com/i.png",
            "mime_type": "image/png",
            "sizes": ["48x48"],
            "theme": "dark",
        }

    def test_copies_by_name_rather_than_forwarding(self):
        # Otherwise a server parks arbitrary JSON in our column.
        from app.services.mcp.probe import _usable_icons

        [icon] = _usable_icons(
            self._server_info(
                {"src": "https://cdn.example.com/i.png", "nonsense": {"deep": [1]}}
            )
        )

        assert set(icon) == {"src", "mime_type", "sizes", "theme"}

    def test_takes_an_inline_image_data_uri(self):
        src = "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="
        assert self._srcs(self._server_info({"src": src})) == [src]

    def test_skips_sources_the_browser_should_not_load(self):
        for bad in (
            "http://cdn.example.com/i.png",       # plaintext
            "javascript:alert(1)",                 # not an image at all
            "data:text/html,<script>x</script>",   # data URI, wrong type
            "file:///etc/passwd",
        ):
            assert self._srcs(self._server_info({"src": bad})) == []

    def test_skips_an_oversize_payload_but_keeps_its_neighbours(self):
        from app.services.mcp.probe import _MAX_ICON_SRC_LEN

        assert self._srcs(
            self._server_info(
                {"src": "data:image/png;base64," + "A" * _MAX_ICON_SRC_LEN},
                {"src": "https://ok.example.com/i.png"},
            )
        ) == ["https://ok.example.com/i.png"]

    def test_the_count_is_bounded(self):
        # Stored and re-serialized into every connector response, so a server
        # cannot make that list as long as it likes.
        from app.services.mcp.probe import _MAX_ICONS

        assert len(
            self._srcs(
                self._server_info(
                    *[
                        {"src": f"https://cdn.example.com/{n}.png"}
                        for n in range(_MAX_ICONS + 5)
                    ]
                )
            )
        ) == _MAX_ICONS

    def test_no_icons_is_not_an_error(self):
        from app.services.mcp.probe import _usable_icons

        assert _usable_icons(self._server_info()) == []
        assert _usable_icons(None) == []


class TestProbePagination:
    """One page is not the catalog — the worker paginates, so a probe that
    stops at page one caches tools the user can't see or switch off."""

    class _PagingSession:
        def __init__(self, pages):
            self._pages = pages
            self.cursors_seen = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return SimpleNamespace(
                serverInfo=SimpleNamespace(name="Paged", icons=None)
            )

        async def list_tools(self, cursor=None):
            self.cursors_seen.append(cursor)
            names, next_cursor = self._pages[len(self.cursors_seen) - 1]
            return SimpleNamespace(
                tools=[SimpleNamespace(name=n, description="") for n in names],
                nextCursor=next_cursor,
            )

    async def test_every_page_is_collected(self, monkeypatch):
        from app.models.mcp_server import MCPServerAuthType
        from app.services.mcp import probe as probe_mod

        session = self._PagingSession(
            [(["search"], "c1"), (["delete_account"], None)]
        )
        _install(monkeypatch, session)

        result = await probe_mod.probe(
            "https://mcp.example.com/mcp", MCPServerAuthType.NONE, None
        )

        assert [tool["name"] for tool in result.tools] == ["search", "delete_account"]
        assert session.cursors_seen == [None, "c1"]

    async def test_a_repeated_cursor_is_refused_rather_than_truncated(
        self, monkeypatch,
    ):
        # Storing what arrived would be indistinguishable from a whole catalog:
        # the card would read healthy while the tools past the loop are simply
        # gone, with nothing saying which or why.
        from app.errors import ConnectorUnreachableError
        from app.models.mcp_server import MCPServerAuthType
        from app.services.mcp import probe as probe_mod

        session = self._PagingSession([(["a"], "same"), (["b"], "same")])
        _install(monkeypatch, session)

        with pytest.raises(ConnectorUnreachableError):
            await probe_mod.probe(
                "https://mcp.example.com/mcp", MCPServerAuthType.NONE, None
            )


class TestStoredListingIsSwitchable:
    """``tools_preview`` exists to be rendered and switched off, so it is
    bounded by what ``disabled_tools`` will accept rather than by bytes."""

    class _OneShotSession(TestProbePagination._PagingSession):
        def __init__(self, tools):
            super().__init__([(None, None)])
            self._tools = tools

        async def list_tools(self, cursor=None):
            self.cursors_seen.append(cursor)
            return SimpleNamespace(tools=self._tools, nextCursor=None)

    async def _probe(self, monkeypatch, tools):
        from app.models.mcp_server import MCPServerAuthType
        from app.services.mcp import probe as probe_mod

        _install(monkeypatch, self._OneShotSession(tools))
        return await probe_mod.probe(
            "https://mcp.example.com/mcp", MCPServerAuthType.NONE, None
        )

    async def test_an_unswitchable_name_is_dropped(self, monkeypatch, caplog):
        # Past the limit the schema rejects, so it could only ever render a
        # switch that cannot be thrown.
        from app.schemas.mcp_server import MAX_TOOL_NAME_LEN

        result = await self._probe(monkeypatch, [
            SimpleNamespace(name="search", description="ok"),
            SimpleNamespace(name="x" * (MAX_TOOL_NAME_LEN + 1), description=""),
        ])

        assert [tool["name"] for tool in result.tools] == ["search"]

    async def test_a_long_description_is_truncated_not_dropped(self, monkeypatch):
        # Unlike axon, which needs a whole schema or none, this copy only reads.
        from app.services.mcp.probe import _MAX_DESCRIPTION_CHARS

        result = await self._probe(monkeypatch, [
            SimpleNamespace(name="search", description="d" * 10_000),
        ])

        assert len(result.tools) == 1
        assert len(result.tools[0]["description"]) == _MAX_DESCRIPTION_CHARS

    async def test_the_listing_stops_at_the_switchable_count(self, monkeypatch):
        from app.schemas.mcp_server import MAX_DISABLED_TOOLS

        result = await self._probe(monkeypatch, [
            SimpleNamespace(name=f"t{index}", description="")
            for index in range(MAX_DISABLED_TOOLS + 50)
        ])

        assert len(result.tools) == MAX_DISABLED_TOOLS

    async def test_an_ordinary_catalog_is_untouched(self, monkeypatch):
        result = await self._probe(monkeypatch, [
            SimpleNamespace(name="search", description="Find things"),
            SimpleNamespace(name="create", description="Make things"),
        ])

        assert result.tools == [
            {"name": "search", "description": "Find things"},
            {"name": "create", "description": "Make things"},
        ]


def _install(monkeypatch, session):
    """Point ``probe`` at *session*, bypassing the guard and the transport."""
    async def _guard_ok(url):
        return None

    @asynccontextmanager
    async def fake_shc(*args, **kwargs):
        yield ("r", "w", lambda: None)

    monkeypatch.setattr(probe_mod, "guard_public_url", _guard_ok)
    monkeypatch.setattr(probe_mod, "streamablehttp_client", fake_shc)
    monkeypatch.setattr(probe_mod, "ClientSession", lambda read, write: session)
