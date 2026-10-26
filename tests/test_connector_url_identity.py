"""One spelling per endpoint, so the unique index can do its job.

``uq_mcp_server_user_id_url`` compares exact strings. Everything here is a
pair a server cannot tell apart but the index would — which is the whole
reason the normalizer exists.

The second class of test is the more important one: what it must *not* touch.
A normalizer that reached too far would quietly point a Connector somewhere
else, and nothing downstream would notice.
"""

import pytest

from app.schemas.mcp_server import (
    ConnectionTestRequest,
    ConnectorCreate,
    ConnectorUpdate,
)
from app.services.connector.url_identity import normalize_connector_url

CANONICAL = "https://mcp.notion.com/mcp"


class TestSpellingsOfOneEndpoint:
    @pytest.mark.parametrize(
        "written",
        [
            "HTTPS://mcp.notion.com/mcp",
            "https://MCP.Notion.COM/mcp",
            "https://mcp.notion.com:443/mcp",
            "https://mcp.notion.com/mcp#section",
            "  https://mcp.notion.com/mcp  ",
        ],
    )
    def test_all_reduce_to_one(self, written):
        assert normalize_connector_url(written) == CANONICAL

    def test_a_bare_host_and_its_slash_agree(self):
        assert normalize_connector_url("https://mcp.notion.com/") == (
            normalize_connector_url("https://mcp.notion.com")
        )


class TestWhatItMustNotTouch:
    def test_a_trailing_slash_on_a_path_is_kept(self):
        # The connection follows no redirect, so a server that serves ``/mcp/``
        # and answers ``/mcp`` with a 307 is reachable only as written.
        assert normalize_connector_url("https://x.com/mcp/") == "https://x.com/mcp/"

    def test_the_path_keeps_its_case(self):
        # Paths are case-sensitive (RFC 3986 §6.2.2.1). Folding them would
        # point a Connector at a different resource.
        assert normalize_connector_url("https://x.com/MCP") == "https://x.com/MCP"

    def test_the_query_survives_whole(self):
        # Order and repeats can matter to a server, so it is copied, not sorted.
        url = "https://x.com/mcp?b=2&a=1&a=3"
        assert normalize_connector_url(url) == url

    def test_a_non_default_port_stays(self):
        assert normalize_connector_url("https://x.com:8443/mcp") == ("https://x.com:8443/mcp")

    def test_the_default_port_is_read_per_scheme(self):
        # 443 is default for https and *not* for http; dropping it from both
        # would rewrite where an http URL points.
        assert normalize_connector_url("http://x.com:443/mcp") == ("http://x.com:443/mcp")
        assert normalize_connector_url("http://x.com:80/mcp") == "http://x.com/mcp"

    def test_an_ipv6_literal_keeps_its_brackets(self):
        assert normalize_connector_url("https://[::1]:8443/mcp") == ("https://[::1]:8443/mcp")

    def test_credentials_in_the_url_are_carried_through(self):
        # Dropping them would turn an authenticated endpoint into an anonymous
        # one that fails at connect time, blamed on the server.
        assert normalize_connector_url("https://user:pw@x.com/mcp") == ("https://user:pw@x.com/mcp")


class TestMalformedInputIsLeftToItsJudge:
    @pytest.mark.parametrize("written", ["not a url", "https://x.com:notaport/mcp", "", "/mcp"])
    def test_it_comes_back_as_written(self, written):
        # Rejecting belongs to ``url_guard`` and the field constraints. A
        # normalizer that also judged would give them somewhere to disagree.
        assert normalize_connector_url(written) == written.strip()


class TestEveryWayInGoesThroughIt:
    """On the type, not at the call sites — five of those write a URL."""

    def test_creating_a_connector(self):
        assert ConnectorCreate(name="n", url="HTTPS://X.com/mcp").url == ("https://x.com/mcp")

    def test_editing_one(self):
        assert ConnectorUpdate(url="https://x.com:443/mcp#f").url == ("https://x.com/mcp")

    def test_testing_a_connection(self):
        # The probe too: a test that succeeded against one spelling and a row
        # stored under another is a confusing way to fail later.
        assert ConnectionTestRequest(url="https://X.com/mcp#f").url == ("https://x.com/mcp")

    def test_an_omitted_url_stays_omitted(self):
        assert ConnectorUpdate().url is None
