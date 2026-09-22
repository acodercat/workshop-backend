"""Probe a remote MCP server: auth headers, success mapping, failure paths."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from anyio import EndOfStream
from mcp.types import INTERNAL_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND

from app.errors import (
    ConnectorAuthRejectedError,
    ConnectorHttpStatusError,
    ConnectorNotMcpEndpointError,
    ConnectorTlsFailedError,
    ConnectorUnreachableError,
    InvalidConnectorUrlError,
)
from app.models.mcp_server import MCPServerAuthType
from app.services.mcp import probe as probe_mod
from app.services.mcp.probe import _describe, _unreachable, build_auth_headers, probe


def _mcp_error(code: int, message: str) -> BaseException:
    """A failure shaped the way the SDK delivers a non-MCP answer: not an HTTP
    exception, but an MCP-level error raised by the session."""
    from mcp.shared.exceptions import MCPError

    return ExceptionGroup("g", [MCPError(code=code, message=message)])


def _with_status(status_code: int) -> BaseException:
    """A failure carrying an HTTP response, shaped the way httpx's is."""

    class _Response:
        def __init__(self, code: int) -> None:
            self.status_code = code

    error = RuntimeError(f"Server returned {status_code}")
    error.response = _Response(status_code)  # type: ignore[attr-defined]
    return ExceptionGroup("g", [error])


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

    # ``icons`` mirrors the real ``Implementation``; leaving it off would only
    # prove the code tolerates a fake that can't occur.
    server_info = SimpleNamespace(name="Demo Server", icons=None)
    # Every fake here serves tools, so every one advertises them: ``probe``
    # skips the listing entirely for a server that does not.
    server_capabilities = SimpleNamespace(tools=SimpleNamespace())

    async def list_tools(self, cursor=None):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name="search", description="Search things"),
                SimpleNamespace(name="create", description=None),
            ],
            next_cursor=None,
        )


class TestProbe:
    async def test_success_maps_name_and_tools(self, monkeypatch):
        async def _guard_ok(url):
            return None

        monkeypatch.setattr(probe_mod, "guard_public_url", _guard_ok)

        monkeypatch.setattr(probe_mod, "guarded_client", _fake_http_client)
        monkeypatch.setattr(probe_mod, "streamable_http_client", _fake_transport)
        monkeypatch.setattr(probe_mod, "Client", lambda *args, **kwargs: _FakeSession())

        result = await probe("https://mcp.example.com/x", MCPServerAuthType.NONE, None)

        assert result.server_name == "Demo Server"
        assert result.tools == [
            {"name": "search", "description": "Search things"},
            {"name": "create", "description": ""},
        ]

    async def test_it_negotiates_the_protocol_rather_than_assuming_one(self, monkeypatch):
        # The reason this backend is on the 2.x SDK at all. ``mode="auto"``
        # probes ``server/discover`` and falls back to ``initialize``; pinned to
        # ``initialize`` it would refuse every server that speaks only the
        # 2026-07-28 stateless protocol — which is the class of server that
        # could not be added before, and the failure would look like an
        # unreachable host rather than a protocol mismatch.
        #
        # Asserted on the argument because the behaviour behind it needs a real
        # stateless server to observe, and there is none in a unit test.
        captured: dict = {}

        async def _guard_ok(url):
            return None

        def _capture(*args, **kwargs):
            captured.update(kwargs)
            return _FakeSession()

        monkeypatch.setattr(probe_mod, "guard_public_url", _guard_ok)
        monkeypatch.setattr(probe_mod, "guarded_client", _fake_http_client)
        monkeypatch.setattr(probe_mod, "streamable_http_client", _fake_transport)
        monkeypatch.setattr(probe_mod, "Client", _capture)

        await probe("https://mcp.example.com/x", MCPServerAuthType.NONE, None)

        assert captured["mode"] == "auto"
        # A probe answers "what is there now"; a cached page-one would make the
        # re-list button a no-op against a server that had changed.
        assert captured["cache"] is None

    async def test_a_server_without_tools_lists_none_rather_than_failing(self, monkeypatch):
        # Asking a server that never advertised the capability earns ``-32601``,
        # which this module reports as the server being unreachable — sending
        # the user to debug a connection that is working.
        class _Toolless(_FakeSession):
            server_capabilities = SimpleNamespace(tools=None)

            async def list_tools(self, cursor=None):
                raise AssertionError("asked a server that never advertised tools")

        _install(monkeypatch, _Toolless())

        result = await probe("https://mcp.example.com/x", MCPServerAuthType.NONE, None)

        assert result.tools == []
        assert result.server_name == "Demo Server"

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

        monkeypatch.setattr(probe_mod, "guarded_client", _fake_http_client)
        monkeypatch.setattr(probe_mod, "streamable_http_client", boom)
        with pytest.raises(ConnectorUnreachableError):
            await probe("https://mcp.example.com/x", MCPServerAuthType.NONE, None)


class TestTheFailureSaysWhatFailed:
    """anyio runs the SDK's transport in a task group, so every failure inside
    it used to reach the user as ``str(ExceptionGroup)`` — "unhandled errors in
    a TaskGroup (1 sub-exception)" — identically for a rejected credential, a
    timeout, and a server that is not MCP at all. A real report of the last one
    was unreadable because of it."""

    def test_a_group_reports_its_leaf_not_the_group(self):
        leaf = RuntimeError("All connection attempts failed")
        group = ExceptionGroup("unhandled errors in a TaskGroup", [leaf])
        assert _describe(group) == "All connection attempts failed"

    def test_nested_groups_are_flattened(self):
        # The SDK wraps twice: a group inside a group is what the real trace had.
        leaf = RuntimeError("Server returned an error response")
        nested = ExceptionGroup("outer", [ExceptionGroup("inner", [leaf])])
        assert _describe(nested) == "Server returned an error response"

    def test_a_cause_chain_is_followed(self):
        inner = RuntimeError("the real reason")
        outer = RuntimeError("")
        outer.__cause__ = inner
        assert _describe(ExceptionGroup("g", [outer])) == "the real reason"

    def test_only_the_first_line_survives(self):
        # A pydantic ValidationError is eleven stanzas naming every member of
        # the JSON-RPC union; none of them says more than the first line.
        leaf = RuntimeError(
            "Failed to parse JSON response: 11 validation errors\nJSONRPC.jsonrpc\n  ..."
        )
        assert _describe(leaf) == "Failed to parse JSON response: 11 validation errors"

    def test_a_self_referential_context_terminates(self):
        # The message has to be empty, or ``_describe`` returns on this leaf
        # before the cycle is ever walked and the guard goes untested.
        exc = RuntimeError("")
        exc.__context__ = exc
        assert _describe(exc) == "RuntimeError"

    def test_a_leaf_with_no_message_falls_back_to_its_type(self):
        # anyio's stream exceptions all stringify to "" and this transport
        # traffics in them; returning str() of the group for those put the
        # TaskGroup sentence straight back.
        group = ExceptionGroup("unhandled errors in a TaskGroup", [EndOfStream()])
        assert _describe(group) == "EndOfStream"

    def test_a_group_context_is_followed(self):
        # Raising a group from __aexit__ while another exception is in flight
        # leaves that one in the group's __context__.
        group = ExceptionGroup("g", [EndOfStream()])
        group.__context__ = RuntimeError("the real reason")
        assert _describe(group) == "the real reason"

    def test_siblings_are_preferred_over_contexts(self):
        # Breadth-first: an unrelated in-flight exception must not outrank a
        # direct member that has something to say.
        sibling = RuntimeError("what actually failed")
        group = ExceptionGroup("g", [sibling])
        group.__context__ = RuntimeError("unrelated, merely in flight")
        assert _describe(group) == "what actually failed"

    def test_a_non_mcp_answer_names_the_endpoint_to_check(self):
        # The reported failure: the server answered 200 with its own
        # {"code":404,"msg":"请求地址不存在:sse"} envelope, which is not JSON-RPC.
        group = ExceptionGroup(
            "g", [RuntimeError("Failed to parse JSON response: ...")]
        )
        error = _unreachable(group, handshook=False)
        assert "/mcp" in str(error)
        assert "/sse" in str(error)

    def test_the_endpoint_hint_stops_once_the_session_is_open(self):
        # Same failure, but during tools/list: the endpoint already proved it
        # speaks Streamable HTTP, so telling the user to change the URL would
        # send them the wrong way.
        group = ExceptionGroup(
            "g", [RuntimeError("Failed to parse JSON response: ...")]
        )
        error = _unreachable(group, handshook=True)
        assert "/mcp" not in str(error)
        assert str(error).startswith("Could not reach the MCP server:")

    def test_tls_failure_is_its_own_type(self):
        # The address and the port are right; a certificate has to change.
        # The generic "check the URL and authentication" sends people to look
        # at the two things that are not the problem.
        #
        # Nested the way it actually arrives — the transport raises its own
        # connect error and the SSL failure is its ``__cause__``, inside the
        # anyio group. A bare leaf would pass without proving the classifier
        # reaches through the cause chain, which is the only thing that makes
        # this branch fire in production.
        import ssl

        connect_error = RuntimeError("All connection attempts failed")
        connect_error.__cause__ = ssl.SSLCertVerificationError(
            "self signed certificate"
        )
        error = _unreachable(ExceptionGroup("g", [connect_error]), handshook=False)
        assert isinstance(error, ConnectorTlsFailedError)
        assert error.error_type == "connector_tls_failed"

    def test_a_rejected_credential_is_not_a_connection_problem(self):
        # 401/403 is the one failure here fixed by editing a credential rather
        # than an address.
        for status_code in (401, 403):
            error = _unreachable(_with_status(status_code), handshook=False)
            assert isinstance(error, ConnectorAuthRejectedError)
            assert str(status_code) in str(error)

    def test_a_refused_status_outranks_the_sdk_stand_in(self):
        # How a 401 actually arrives: the transport does not raise for it, it
        # answers with INTERNAL_ERROR and the sentence "Server returned an
        # error response" — the same thing a 500 produces, status dropped.
        # Without the status recorded off the wire, a wrong key reads as a
        # broken server and the advice is to go check the address.
        for status_code in (401, 403):
            error = _unreachable(
                _mcp_error(INTERNAL_ERROR, "Server returned an error response"),
                handshook=False,
                refused=[status_code],
            )
            assert isinstance(error, ConnectorAuthRejectedError)
            assert str(status_code) in str(error)

    def test_a_refused_5xx_is_still_the_server_failing(self):
        error = _unreachable(
            _mcp_error(INTERNAL_ERROR, "Server returned an error response"),
            handshook=False,
            refused=[500],
        )
        assert isinstance(error, ConnectorHttpStatusError)

    def test_an_http_error_says_something_answered(self):
        # Something is listening and did reply, so the network and the port are
        # fine — which is the opposite of what "unreachable" tells the user.
        error = _unreachable(_with_status(404), handshook=False)
        assert isinstance(error, ConnectorHttpStatusError)
        assert "404" in str(error)

    def test_a_non_mcp_answer_has_its_own_type(self):
        group = ExceptionGroup(
            "g", [RuntimeError("Failed to parse JSON response: ...")]
        )
        assert isinstance(
            _unreachable(group, handshook=False), ConnectorNotMcpEndpointError
        )

    def test_a_server_that_answers_html_is_a_wrong_endpoint(self):
        # The reported case: a reachable address without its /mcp suffix. The
        # transport raises nothing for it — it turns the answer into a JSON-RPC
        # error and the session raises *that* — so none of the HTTP-exception
        # checks see it, and it arrived as the generic "cannot connect" while
        # the fix was one word in the URL.
        error = _unreachable(
            _mcp_error(INVALID_REQUEST, "Unexpected content type: text/html"),
            handshook=False,
        )
        assert isinstance(error, ConnectorNotMcpEndpointError)
        assert "/mcp" in str(error)

    def test_a_404_reads_as_a_wrong_endpoint_not_a_server_fault(self):
        # The SDK spells a 404-with-no-session as METHOD_NOT_FOUND: a gateway
        # or a web root that never heard of this method. Matched on the code,
        # which is the protocol's, rather than on the SDK's wording.
        error = _unreachable(_mcp_error(METHOD_NOT_FOUND, "Not Found"), handshook=False)
        assert isinstance(error, ConnectorNotMcpEndpointError)

    def test_a_server_error_stays_a_server_error(self):
        error = _unreachable(
            _mcp_error(INTERNAL_ERROR, "Server returned an error response"),
            handshook=False,
        )
        assert isinstance(error, ConnectorHttpStatusError)

    def test_the_same_codes_after_the_handshake_are_not_a_wrong_endpoint(self):
        # Once the session is open the endpoint has proved it speaks Streamable
        # HTTP; the same codes then mean a server misbehaving mid-catalogue, and
        # sending the user to edit a correct URL is the wrong advice.
        for code, message in (
            (INVALID_REQUEST, "Unexpected content type: text/html"),
            (METHOD_NOT_FOUND, "Not Found"),
            (INTERNAL_ERROR, "Server returned an error response"),
        ):
            error = _unreachable(_mcp_error(code, message), handshook=True)
            assert type(error) is ConnectorUnreachableError

    def test_every_subclass_is_still_caught_as_unreachable(self):
        # ``example_drafts`` and the probe's own re-raise catch the base class.
        # Siblings rather than subclasses would have slipped past both.
        import ssl

        for exc in (
            ExceptionGroup("g", [ssl.SSLError("x")]),
            _with_status(401),
            _with_status(500),
            ExceptionGroup("g", [RuntimeError("Failed to parse JSON response")]),
        ):
            assert isinstance(
                _unreachable(exc, handshook=False), ConnectorUnreachableError
            )

    def test_the_raw_detail_reaches_the_log(self, caplog):
        # The client is shown a sentence built from the error *type* and never
        # this string, so without the log line the only description of what
        # actually happened is discarded at the browser.
        with caplog.at_level("WARNING"):
            _unreachable(
                ExceptionGroup("g", [RuntimeError("connection refused")]),
                handshook=False,
            )
        assert "connection refused" in caplog.text
        assert "connector_unreachable" in caplog.text

    def test_other_failures_keep_the_plain_wording(self):
        error = _unreachable(
            ExceptionGroup("g", [RuntimeError("timed out")]), handshook=False
        )
        assert str(error) == "Could not reach the MCP server: timed out"

    def test_no_failure_ever_reports_an_empty_reason(self):
        # A dangling "Could not reach the MCP server: " is the same unusable
        # message in a different costume.
        for exc in (RuntimeError(""), ExceptionGroup("g", [EndOfStream()])):
            assert (
                str(_unreachable(exc, handshook=False)).strip().endswith((":",))
                is False
            )


class TestServerIcons:
    """``server_info.icons`` (SEP-973) gives a connector its own artwork. Every
    source the browser can load directly is kept, because which one is best
    depends on the device and the colour scheme — and only the browser knows
    those."""

    def _server_info(self, *icons):
        """A ``server_info`` whose icons are the given dicts, SDK-shaped."""
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
            self._server_info(
                {
                    "src": "https://cdn.example.com/i.png",
                    "mime_type": "image/png",
                    "sizes": ["48x48"],
                    # Not modelled by the SDK, so it arrives as an extra field —
                    # dropping it would take dark-mode support with it.
                    "theme": "dark",
                }
            )
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
            self._server_info({"src": "https://cdn.example.com/i.png", "nonsense": {"deep": [1]}})
        )

        assert set(icon) == {"src", "mime_type", "sizes", "theme"}

    def test_takes_an_inline_image_data_uri(self):
        src = "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="
        assert self._srcs(self._server_info({"src": src})) == [src]

    def test_skips_sources_the_browser_should_not_load(self):
        for bad in (
            "http://cdn.example.com/i.png",  # plaintext
            "javascript:alert(1)",  # not an image at all
            "data:text/html,<script>x</script>",  # data URI, wrong type
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

        assert (
            len(
                self._srcs(
                    self._server_info(
                        *[
                            {"src": f"https://cdn.example.com/{n}.png"}
                            for n in range(_MAX_ICONS + 5)
                        ]
                    )
                )
            )
            == _MAX_ICONS
        )

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

        server_info = SimpleNamespace(name="Paged", icons=None)
        server_capabilities = SimpleNamespace(tools=SimpleNamespace())

        async def list_tools(self, cursor=None):
            self.cursors_seen.append(cursor)
            names, next_cursor = self._pages[len(self.cursors_seen) - 1]
            return SimpleNamespace(
                tools=[SimpleNamespace(name=n, description="") for n in names],
                next_cursor=next_cursor,
            )

    async def test_every_page_is_collected(self, monkeypatch):
        from app.models.mcp_server import MCPServerAuthType
        from app.services.mcp import probe as probe_mod

        session = self._PagingSession([(["search"], "c1"), (["delete_account"], None)])
        _install(monkeypatch, session)

        result = await probe_mod.probe("https://mcp.example.com/mcp", MCPServerAuthType.NONE, None)

        assert [tool["name"] for tool in result.tools] == ["search", "delete_account"]
        assert session.cursors_seen == [None, "c1"]

    async def test_a_repeated_cursor_is_refused_rather_than_truncated(
        self,
        monkeypatch,
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
            await probe_mod.probe("https://mcp.example.com/mcp", MCPServerAuthType.NONE, None)


class TestStoredListingIsSwitchable:
    """``tools_preview`` exists to be rendered and switched off, so it is
    bounded by what ``disabled_tools`` will accept rather than by bytes."""

    class _OneShotSession(TestProbePagination._PagingSession):
        def __init__(self, tools):
            super().__init__([(None, None)])
            self._tools = tools

        async def list_tools(self, cursor=None):
            self.cursors_seen.append(cursor)
            return SimpleNamespace(tools=self._tools, next_cursor=None)

    async def _probe(self, monkeypatch, tools):
        from app.models.mcp_server import MCPServerAuthType
        from app.services.mcp import probe as probe_mod

        _install(monkeypatch, self._OneShotSession(tools))
        return await probe_mod.probe("https://mcp.example.com/mcp", MCPServerAuthType.NONE, None)

    async def test_an_unswitchable_name_is_dropped(self, monkeypatch, caplog):
        # Past the limit the schema rejects, so it could only ever render a
        # switch that cannot be thrown.
        from app.schemas.mcp_server import MAX_TOOL_NAME_LEN

        result = await self._probe(
            monkeypatch,
            [
                SimpleNamespace(name="search", description="ok"),
                SimpleNamespace(name="x" * (MAX_TOOL_NAME_LEN + 1), description=""),
            ],
        )

        assert [tool["name"] for tool in result.tools] == ["search"]

    async def test_a_long_description_is_truncated_not_dropped(self, monkeypatch):
        # Unlike axon, which needs a whole schema or none, this copy only reads.
        from app.services.mcp.probe import _MAX_DESCRIPTION_CHARS

        result = await self._probe(
            monkeypatch,
            [
                SimpleNamespace(name="search", description="d" * 10_000),
            ],
        )

        assert len(result.tools) == 1
        assert len(result.tools[0]["description"]) == _MAX_DESCRIPTION_CHARS

    async def test_the_listing_stops_at_the_switchable_count(self, monkeypatch):
        from app.schemas.mcp_server import MAX_DISABLED_TOOLS

        result = await self._probe(
            monkeypatch,
            [
                SimpleNamespace(name=f"t{index}", description="")
                for index in range(MAX_DISABLED_TOOLS + 50)
            ],
        )

        assert len(result.tools) == MAX_DISABLED_TOOLS

    async def test_an_ordinary_catalog_is_untouched(self, monkeypatch):
        result = await self._probe(
            monkeypatch,
            [
                SimpleNamespace(name="search", description="Find things"),
                SimpleNamespace(name="create", description="Make things"),
            ],
        )

        assert result.tools == [
            {"name": "search", "description": "Find things"},
            {"name": "create", "description": "Make things"},
        ]


@asynccontextmanager
async def _fake_transport(*args, **kwargs):
    """Two streams, which is all ``Client`` asks a transport to yield."""
    yield ("r", "w")


@asynccontextmanager
async def _fake_http_client(*args, **kwargs):
    """The guarded client ``probe`` now builds and owns itself.

    ``event_hooks`` because the real one has it and ``probe`` appends its
    refused-status recorder to it — a fake missing the attribute fails every
    probe with an ``AttributeError`` dressed up as an unreachable server.
    """
    yield SimpleNamespace(event_hooks={"request": [], "response": []})


def _install(monkeypatch, session):
    """Point ``probe`` at *session*, bypassing the guard and the transport.

    Three names where 1.x needed two: the SDK stopped taking a client factory,
    so ``probe`` builds the guarded client itself and hands the finished one
    over — and that client is entered here too, or the ``async with`` around it
    would have nothing to enter.
    """

    async def _guard_ok(url):
        return None

    monkeypatch.setattr(probe_mod, "guard_public_url", _guard_ok)
    monkeypatch.setattr(probe_mod, "guarded_client", _fake_http_client)
    monkeypatch.setattr(probe_mod, "streamable_http_client", _fake_transport)
    monkeypatch.setattr(probe_mod, "Client", lambda *args, **kwargs: session)
