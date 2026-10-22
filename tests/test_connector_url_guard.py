"""SSRF guard for user-supplied Connector URLs."""

# ``httpx2 as httpx`` deliberately: this file drives the SSRF transport, which
# is httpx2's since the SDK bump. Aliased rather than renamed at ~30 call sites
# so no line can be missed — a half-converted file would pass green, since the
# two libraries are duck-compatible across everything asserted here and pytest
# files are outside mypy's scope.
import httpx2 as httpx
import pytest

from app.errors import InvalidConnectorUrlError
from app.services.mcp import url_guard

# The one refusal for a host that is unknown or internal, which names no address.
_NOT_PUBLIC = "The host 'rebind.example.com' does not resolve to a public address."


class TestValidatePublicUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://mcp.example.com/x",  # not https
            "https:///x",  # no host
            "https://127.0.0.1/x",  # loopback
            "https://10.0.0.5/x",  # private
            "https://192.168.1.1/x",  # private
            "https://169.254.169.254/x",  # cloud metadata / link-local
            # Shared address space (RFC 6598). Neither ``is_private`` nor
            # ``is_loopback``, so a blocklist waves it through — and it is where
            # Tailscale, carrier-grade NAT and plenty of internal services live.
            "https://100.64.0.10/x",
            "https://192.0.0.1/x",  # IETF protocol assignments
            "https://198.18.0.1/x",  # benchmarking range
            "https://[fc00::1]/x",  # IPv6 unique-local
            # An IPv4 address wrapped in a globally-assigned IPv6 prefix. ``is_global``
            # judges the outer address and says yes; on a NAT64/DNS64 network the
            # translator then dials the inner one, which here is 127.0.0.1.
            "https://[64:ff9b::7f00:1]/x",  # NAT64 well-known (RFC 6052)
            "https://[::7f00:1]/x",  # IPv4-compatible, deprecated (RFC 4291)
        ],
    )
    def test_rejects_unsafe(self, url):
        with pytest.raises(InvalidConnectorUrlError):
            url_guard.validate_public_url(url)

    def test_accepts_public(self, monkeypatch):
        monkeypatch.setattr(
            url_guard.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        url_guard.validate_public_url("https://mcp.example.com/x")  # no raise

    def test_unresolvable_host_rejected(self, monkeypatch):
        import socket

        def boom(*args, **kwargs):
            raise socket.gaierror("nope")

        monkeypatch.setattr(url_guard.socket, "getaddrinfo", boom)
        with pytest.raises(InvalidConnectorUrlError) as refused:
            url_guard.validate_public_url("https://rebind.example.com/x")
        assert str(refused.value) == _NOT_PUBLIC

    def test_a_private_answer_is_refused_like_no_answer(self, monkeypatch):
        """Told apart, the two would let a caller map internal names to addresses."""
        monkeypatch.setattr(
            url_guard.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.5", 443))],
        )
        with pytest.raises(InvalidConnectorUrlError) as refused:
            url_guard.validate_public_url("https://rebind.example.com/x")
        assert str(refused.value) == _NOT_PUBLIC


class TestEmbeddedIPv4Prefixes:
    """The wrappers ``is_global`` waves through, and the boundary of that.

    Refusing them is a different kind of refusal from the rest: the address the
    registry judges is global, and the one that gets dialled is inside it.
    """

    @pytest.mark.parametrize(
        "address,dialled",
        [
            ("2002:7f00:0001::", "127.0.0.1"),
            ("2002:0a00:0005::", "10.0.0.5"),
            ("2002:a9fe:a9fe::", "169.254.169.254"),  # cloud metadata
        ],
    )
    def test_a_6to4_wrapper_is_refused(self, address, dialled):
        import ipaddress

        # 6to4 keeps its IPv4 in bits 16–48, not the low 32, so the
        # arithmetic the NAT64 cases rely on never finds it, and whether
        # ``is_global`` covers for that depends on the interpreter: 3.11
        # calls ``2002::/16`` global, 3.13 does not. The guard decodes the
        # address itself rather than inherit the difference — which is why
        # the decoding is asserted and not only the refusal. On 3.13 the
        # refusal below holds either way, so it alone would not notice the
        # decoding being taken out again.
        address_object = ipaddress.ip_address(address)
        assert str(address_object.sixtofour) == dialled
        assert address_object.sixtofour in url_guard._carried_ipv4(address_object)

        with pytest.raises(InvalidConnectorUrlError):
            url_guard.validate_public_url(f"https://[{address}]/x")

    @pytest.mark.parametrize(
        "address,dialled",
        [
            ("64:ff9b::7f00:1", "127.0.0.1"),
            ("64:ff9b::a00:5", "10.0.0.5"),
            ("64:ff9b::a9fe:a9fe", "169.254.169.254"),  # cloud metadata
            ("::7f00:1", "127.0.0.1"),
        ],
    )
    def test_a_wrapped_internal_address_is_refused(self, address, dialled):
        import ipaddress

        # The premise, asserted rather than asserted-about: without the extra
        # check these pass, and what they reach is an internal host.
        assert ipaddress.ip_address(address).is_global
        assert str(ipaddress.ip_address(int(ipaddress.ip_address(address)) & 0xFFFFFFFF)) == dialled

        with pytest.raises(InvalidConnectorUrlError):
            url_guard.validate_public_url(f"https://[{address}]/x")

    @pytest.mark.parametrize(
        "address,dialled",
        [
            ("64:ff9b::8.8.8.8", "8.8.8.8"),
            ("64:ff9b::1.1.1.1", "1.1.1.1"),
        ],
    )
    def test_a_wrapped_public_address_is_allowed(self, address, dialled):
        import ipaddress

        # The prefix is not the answer, the address inside it is. DNS64 answers
        # for every IPv4-only host out of this prefix, so refusing the prefix
        # would leave an IPv6-only deployment with no reachable Connector at all.
        assert str(ipaddress.ip_address(int(ipaddress.ip_address(address)) & 0xFFFFFFFF)) == dialled

        url_guard.validate_public_url(f"https://[{address}]/x")

    def test_an_ordinary_global_ipv6_address_still_passes(self, monkeypatch):
        # The boundary. Refusing whole swathes of IPv6 to close this would cost
        # every legitimate v6-only MCP server.
        monkeypatch.setattr(
            url_guard.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (None, None, None, None, ("2606:4700::6810:85e5", 443, 0, 0)),
            ],
        )
        url_guard.validate_public_url("https://mcp.example.com/x")


class TestPinnedResolveTransport:
    async def test_rejects_rebind_to_private_ip(self, monkeypatch):
        # The DNS-pinning transport re-resolves at connect time and rejects a
        # host that resolves to a private IP — closing the rebinding TOCTOU a
        # pre-flight check alone leaves open. (loop.getaddrinfo delegates to
        # socket.getaddrinfo, so patching it drives the async resolution.)
        monkeypatch.setattr(
            url_guard.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.5", 443))],
        )
        transport = url_guard._PinnedResolveTransport()
        with pytest.raises(InvalidConnectorUrlError) as refused:
            await transport.handle_async_request(
                httpx.Request("GET", "https://rebind.example.com/")
            )
        assert str(refused.value) == _NOT_PUBLIC

    async def test_one_private_answer_among_public_ones_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            url_guard.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("10.0.0.5", 443)),
            ],
        )

        async def connect(self, request, **kwargs):
            raise AssertionError(f"connected to {request.url.host}")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", connect)
        transport = url_guard._PinnedResolveTransport()
        with pytest.raises(InvalidConnectorUrlError) as refused:
            await transport.handle_async_request(
                httpx.Request("GET", "https://rebind.example.com/")
            )
        assert str(refused.value) == _NOT_PUBLIC

    async def test_falls_over_to_the_next_address(self, monkeypatch):
        # A CDN name answers with several addresses and ``getaddrinfo`` rotates
        # their order per call, so pinning the first one alone failed requests at
        # random — one unreachable edge, or an IPv6 record on a host with no IPv6
        # route, and the next request would succeed where this one did not. An
        # ordinary client tries each; pinning must not cost that.
        monkeypatch.setattr(
            url_guard.socket,
            "getaddrinfo",
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
            url_guard.socket,
            "getaddrinfo",
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
            url_guard.asyncio.get_running_loop().__class__,
            "getaddrinfo",
            _resolving_to("10.0.0.5"),
            raising=False,
        )
        async with url_guard.guarded_client() as client:
            with pytest.raises(InvalidConnectorUrlError):
                await client.post("https://internal.example/oauth/token", data={})


def _resolving_to(address: str):
    async def _getaddrinfo(self, host, port, **kwargs):
        return [(2, 1, 6, "", (address, port))]

    return _getaddrinfo
