"""SSRF guard for user-supplied Connector URLs.

A Connector URL is attacker-controlled and we connect to it from server-side
infrastructure, so it must be a public ``https`` endpoint. This rejects
non-https schemes and any host that resolves to a private / loopback /
link-local / reserved address (including the cloud metadata IP
169.254.169.254, which falls in link-local).

Two layers of defense:

- :func:`validate_public_url` / :func:`guard_public_url` — a pre-flight check
  (https scheme + resolved IPs are public) that yields a clean 400 on a bad URL.
- :func:`guarded_client` — an httpx client whose transport resolves each request
  host **once**, rejects non-public IPs, and **pins** the connection to that IP,
  so a second DNS lookup can't rebind to an internal address between check and
  connect (the TOCTOU window a pre-flight check alone leaves open). It also never
  follows redirects, so a public URL can't 3xx-bounce onto an internal one. Pass
  it as the MCP SDK's ``httpx_client_factory`` (probe) or use it directly (OAuth).
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from app.errors import InvalidConnectorUrlError

_DEFAULT_TIMEOUT = 30.0


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
        addr_infos = socket.getaddrinfo(
            host, parsed.port or 443, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as exc:
        raise InvalidConnectorUrlError(f"Could not resolve host {host!r}.") from exc

    for *_, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not _is_public(ip):
            raise InvalidConnectorUrlError(
                f"Connector URL resolves to a non-public address ({ip})."
            )


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether *ip* is globally routable — an allowlist, not a blocklist.

    ``is_global`` is the IANA special-purpose registry's own answer, which is
    the only one that stays right as that registry grows. Enumerating what to
    reject instead leaves whatever the enumeration missed: shared CGNAT space
    (``100.64.0.0/10``) is neither ``is_private`` nor ``is_loopback``, so a
    blocklist of those waves through Tailscale addresses, carrier-grade NAT
    ranges, and anything else an operator runs in there.
    """
    return ip.is_global


class _PinnedResolveTransport(httpx.AsyncHTTPTransport):
    """httpx transport that resolves each request host once, rejects non-public
    resolved IPs, and pins the connection to that exact IP.

    Pinning closes the DNS-rebinding TOCTOU: the address we validate is the
    address we connect to, so a resolver flip between check and connect can't
    reach an internal host. Host header + TLS SNI stay the original hostname, so
    virtual-host routing and certificate verification are unaffected.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Scheme is enforced here, not only at the pre-flight check: the
        # endpoints a server advertises in its metadata — token, registration,
        # authorization — never pass through that check, and a plaintext one
        # would carry the PKCE verifier, client secret, or refresh token in the
        # clear. MCP requires authorization-server endpoints to be HTTPS.
        if request.url.scheme != "https":
            raise InvalidConnectorUrlError(
                f"Refusing a non-https request to {request.url.host!r}."
            )
        host = request.url.host
        if not host:
            raise InvalidConnectorUrlError("Request URL has no host.")
        port = request.url.port or 443
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except socket.gaierror as exc:
            raise InvalidConnectorUrlError(f"Could not resolve host {host!r}.") from exc

        pinned: str | None = None
        for *_, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if not _is_public(ip):
                raise InvalidConnectorUrlError(
                    f"Connector URL resolves to a non-public address ({ip})."
                )
            if pinned is None:
                pinned = sockaddr[0]
        if pinned is None:
            raise InvalidConnectorUrlError(f"Could not resolve host {host!r}.")

        # Connect to the validated IP, but keep TLS SNI + cert verification on the
        # real hostname (httpcore reads ``sni_hostname``). The ``Host`` header is
        # already the original authority — httpx set it before the transport ran.
        request.extensions["sni_hostname"] = host
        request.url = request.url.copy_with(host=pinned)
        return await super().handle_async_request(request)


def guarded_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """An SSRF-hardened httpx client for outbound Connector/OAuth requests.

    The signature matches the MCP SDK's ``httpx_client_factory``
    (``(headers, timeout, auth) -> AsyncClient``) so it can be passed straight to
    ``streamablehttp_client``; it is equally usable directly. DNS-pinned via
    :class:`_PinnedResolveTransport` and ``follow_redirects=False``.
    """
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout if timeout is not None else httpx.Timeout(_DEFAULT_TIMEOUT),
        auth=auth,
        follow_redirects=False,
        transport=_PinnedResolveTransport(),
    )
