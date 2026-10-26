"""SSRF guard for user-supplied Connector URLs.

A Connector URL is attacker-controlled and we connect to it from server-side
infrastructure, so it must be a public ``https`` endpoint. This rejects
non-https schemes and any host that resolves to a private / loopback /
link-local / reserved address (including the cloud metadata IP
169.254.169.254, which falls in link-local).

Two layers of defense:

- :func:`validate_public_url` / :func:`guard_public_url` — a pre-flight check
  (https scheme + resolved IPs are public) that yields a clean 400 on a bad URL.
- :func:`guarded_client` — an httpx2 client whose transport resolves each request
  host **once**, rejects non-public IPs, and **pins** the connection to that IP,
  so a second DNS lookup can't rebind to an internal address between check and
  connect (the TOCTOU window a pre-flight check alone leaves open). It also never
  follows redirects, so a public URL can't 3xx-bounce onto an internal one. Build
  one and hand it to the MCP SDK (probe) or send through it directly (OAuth).
"""

import asyncio
import ipaddress
import socket
import ssl
from urllib.parse import urlparse

import certifi
import httpx2

from app.errors import InvalidConnectorUrlError

_DEFAULT_TIMEOUT = 30.0

# Trust anchored to certifi rather than to whatever the host ships; see
# :func:`guarded_client`. Built once: the same PEM every time, and a context is
# never changed after this line.
_CERTIFI_TRUST = ssl.create_default_context(cafile=certifi.where())


async def guard_public_url(url: str) -> None:
    """Async wrapper around :func:`validate_public_url`.

    The guard resolves DNS (a blocking syscall); off-load it to a thread so it
    can't stall the shared event loop. ``InvalidConnectorUrlError`` propagates.
    Every server-side outbound URL — the user-supplied Connector URL *and* any
    endpoint derived from a remote server's OAuth metadata — must pass through
    here before we connect to it.
    """
    await asyncio.to_thread(validate_public_url, url)


def validate_public_url(url: str) -> None:
    """Raise :class:`InvalidConnectorUrlError` unless *url* is public https."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise InvalidConnectorUrlError("Connector URL must use https.")
    host = parsed.hostname
    if not host:
        raise InvalidConnectorUrlError("Connector URL has no host.")

    try:
        addr_infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise InvalidConnectorUrlError(_not_public(host)) from exc

    for *_, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not _is_public(ip):
            raise InvalidConnectorUrlError(_not_public(host))


def _not_public(host: str) -> str:
    """One refusal whether *host* is unknown or internal, naming no address: a
    caller who could tell the two apart could map the network this runs in."""
    return f"The host {host!r} does not resolve to a public address."


# IPv6 prefixes that carry an IPv4 address inside them and that ``is_global``
# answers ``True`` for — it judges the *outer* address, and these two are
# globally assigned, so the private address embedded in the low 32 bits is never
# looked at. On a network running NAT64/DNS64 the translator then forwards to
# that address: ``64:ff9b::7f00:1`` is ``127.0.0.1`` and would pass.
#
# Only the prefixes whose IPv4 sits in the low 32 bits are listed, because
# that is the arithmetic below. 6to4 carries its address in bits 16–48 and
# Teredo obfuscates its own, so the stdlib decodes those — see
# :func:`_carried_ipv4`, which is what callers ask.
#
# The address inside is judged, not the prefix refused. On an IPv6-only network
# DNS64 answers for *every* IPv4-only host out of ``64:ff9b::/96``, so refusing
# the prefix outright would leave no reachable Connector at all — and it is the
# inner address that gets dialled, so it is the one the question is about.
_EMBEDDED_IPV4_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64 well-known (RFC 6052 §2.1)
    ipaddress.ip_network("::/96"),  # IPv4-compatible, deprecated (RFC 4291)
)


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether *ip* is globally routable — an allowlist, not a blocklist.

    ``is_global`` is the IANA special-purpose registry's own answer, which is
    the only one that stays right as that registry grows. Enumerating what to
    reject instead leaves whatever the enumeration missed: shared CGNAT space
    (``100.64.0.0/10``) is neither ``is_private`` nor ``is_loopback``, so a
    blocklist of those waves through Tailscale addresses, carrier-grade NAT
    ranges, and anything else an operator runs in there.

    One case it cannot answer alone: a wrapper whose *own* address is global
    while the address that actually gets dialled sits inside it. There the
    question is about the inner one, so every address this one carries is
    asked too. See :func:`_carried_ipv4`.
    """
    if not ip.is_global:
        return False
    return all(carried.is_global for carried in _carried_ipv4(ip))


def _carried_ipv4(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> tuple[ipaddress.IPv4Address, ...]:
    """Every IPv4 address *ip* carries, by any encoding the stdlib knows."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return ()
    carried = [ip.ipv4_mapped, ip.sixtofour, _embedded_ipv4(ip)]
    if ip.teredo is not None:
        carried.extend(ip.teredo)
    return tuple(address for address in carried if address is not None)


def _embedded_ipv4(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """The IPv4 address *ip* carries in its low 32 bits, if it is one of the
    wrappers ``is_global`` judges from the outside. ``None`` for anything else."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if not any(ip in prefix for prefix in _EMBEDDED_IPV4_PREFIXES):
        return None
    return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)


class _PinnedResolveTransport(httpx2.AsyncHTTPTransport):
    """httpx2 transport that resolves each request host once, rejects non-public
    resolved IPs, and pins the connection to that exact IP.

    Pinning closes the DNS-rebinding TOCTOU: the address we validate is the
    address we connect to, so a resolver flip between check and connect can't
    reach an internal host. Host header + TLS SNI stay the original hostname, so
    virtual-host routing and certificate verification are unaffected.

    One consequence of the rewrite: httpcore keys its connection pool on the
    request origin, which now holds the IP — so a client reused across
    *hostnames* that share an IP could reuse a TLS connection whose
    certificate was verified for the other name. Give each client one
    hostname, or send ``Connection: close`` where several must share one.

    OAuth discovery sets that header on its client, but the mitigation does not
    reach every request it makes: the authorization-server metadata GET is built
    by the SDK (``create_oauth_metadata_request``) and sent as-is, never through
    ``client.build_request``, so it carries no client-level headers. Discovery is
    also the one client here that crosses hostnames. Left as it is — both hosts
    are the same untrusted server's to name — but the header is not the guarantee
    it reads as.
    """

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        # Scheme is enforced here, not only at the pre-flight check: the
        # endpoints a server advertises in its metadata — token, registration,
        # authorization — never pass through that check, and a plaintext one
        # would carry the PKCE verifier, client secret, or refresh token in the
        # clear. MCP requires authorization-server endpoints to be HTTPS.
        if request.url.scheme != "https":
            raise InvalidConnectorUrlError(f"Refusing a non-https request to {request.url.host!r}.")
        host = request.url.host
        if not host:
            raise InvalidConnectorUrlError("Request URL has no host.")
        port = request.url.port or 443
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except socket.gaierror as exc:
            raise InvalidConnectorUrlError(_not_public(host)) from exc

        # Every address is checked, and any private one refuses the whole
        # request: a name that answers with a mix is the rebinding case, and
        # picking a public one out of it would be doing the attacker's sorting.
        candidates: list[str] = []
        for *_, sockaddr in infos:
            # Bound once. ``sockaddr[0]`` is the address for every family this
            # can return (AF_INET / AF_INET6); the stdlib types the tuple
            # loosely because other families put a number in that slot.
            address = str(sockaddr[0])
            ip = ipaddress.ip_address(address)
            if not _is_public(ip):
                raise InvalidConnectorUrlError(_not_public(host))
            candidates.append(address)
        if not candidates:
            raise InvalidConnectorUrlError(_not_public(host))

        # Connect to a validated IP, but keep TLS SNI + cert verification on the
        # real hostname (httpcore reads ``sni_hostname``). The ``Host`` header is
        # already the original authority — httpx2 set it before the transport ran.
        request.extensions["sni_hostname"] = host

        # Tried in turn. An ordinary client does that, and replacing the host
        # with one literal address took it away: a name behind a CDN answers with
        # several, ``getaddrinfo`` rotates their order per call, and stopping at
        # the first meant a single unreachable edge — or an IPv6 record on a host
        # with no IPv6 route — failed requests at random while the next one
        # succeeded. Pinning must not cost failover.
        #
        # Only connection failures move on. They happen before any byte of the
        # request is sent, so retrying is safe even for the token exchange; a
        # read failure could mean the server already acted on it, and is raised.
        # Restored afterwards, always. The request object outlives this call: an
        # ``httpx2.Auth`` flow re-yields *the same* one to retry a 401, and a
        # second pass over a URL left holding the pinned literal would read its
        # host as the address — pinning the SNI to an IP no certificate names,
        # and collapsing the candidate list to the one address already tried.
        original = request.url
        last: httpx2.TransportError | None = None
        try:
            for address in candidates:
                request.url = original.copy_with(host=address)
                try:
                    return await super().handle_async_request(request)
                except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
                    last = exc
        finally:
            request.url = original
        # Never with an empty ``last``: the candidate list is non-empty by the
        # check above, so the loop either returned or recorded a failure.
        if last is None:
            raise AssertionError(
                "unreachable: a non-empty candidate list returns or records a failure"
            )
        raise last


def guarded_client(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncClient:
    """An SSRF-hardened httpx2 client for outbound Connector/OAuth requests.

    Keeps the ``(headers, timeout, auth) -> AsyncClient`` shape the SDK's
    ``httpx_client_factory`` had, though 2.x no longer takes a factory — it
    takes the finished client, so every caller now builds one here and passes
    the result. DNS-pinned via :class:`_PinnedResolveTransport` and
    ``follow_redirects=False``.

    **Trust is pinned to certifi, on the transport** — as the worker's copy
    does. httpx2 verifies against the OS trust store by default, so without
    this a Connector's TLS would depend on the host shipping a CA bundle, and
    a slim image would fail every MCP server and nothing else. Nothing is
    lost: the guard refuses any address that is not public, so this client
    only reaches hosts whose certificates chain to a public root. On the
    transport, because a client's ``verify=`` is ignored once a transport is
    supplied. ``SSL_CERT_FILE`` therefore no longer reaches this path.
    """
    return httpx2.AsyncClient(
        headers=headers,
        timeout=timeout if timeout is not None else httpx2.Timeout(_DEFAULT_TIMEOUT),
        auth=auth,
        follow_redirects=False,
        transport=_PinnedResolveTransport(verify=_CERTIFI_TRUST),
    )
