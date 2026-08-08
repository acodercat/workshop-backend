"""One spelling per Connector endpoint.

``uq_mcp_server_user_id_url`` matches on the exact string, so without this
``https://mcp.notion.com/mcp`` and ``https://mcp.notion.com/mcp/`` are two
Connectors for one server — the duplicate the constraint exists to prevent,
walking straight past it.

Only the parts a server cannot tell apart are touched, so normalizing can
never point a URL somewhere else:

- **scheme and host** lowercased. Both are case-insensitive per RFC 3986 §3.1
  and §3.2.2; the path is *not*, and is left exactly as typed.
- **the default port** dropped — ``https://x:443/mcp`` and ``https://x/mcp``
  are the same request on the wire.
- **trailing slashes** trimmed from the path. An MCP endpoint is a resource,
  not a directory.
- **the fragment** dropped. It is never sent to the server, so it can only
  ever be a second name for one endpoint.

Left alone: the query (order and repeats can matter to a server) and
percent-encoding (decoding it would change what is requested).

Malformed input is returned as-is rather than guessed at — rejecting it
belongs to the validators that already do (``url_guard.validate_public_url``
for the scheme, the field constraint for the length), and a normalizer that
also judged would give them two places to disagree.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_connector_url(url: str) -> str:
    """The canonical spelling of *url* — the form stored and compared."""
    trimmed = url.strip()
    parts = urlsplit(trimmed)
    if not parts.scheme or not parts.netloc:
        return trimmed

    try:
        port = parts.port
    except ValueError:
        # A netloc whose port is not a number is malformed; leave it to be
        # rejected rather than reassembling it into something that parses.
        return trimmed

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    # ``hostname`` unwraps an IPv6 literal; ``urlunsplit`` needs it wrapped.
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    if parts.username:
        credentials = parts.username
        if parts.password:
            credentials = f"{credentials}:{parts.password}"
        netloc = f"{credentials}@{netloc}"

    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, ""))
