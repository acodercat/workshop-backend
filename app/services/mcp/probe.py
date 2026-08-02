"""Probe a remote MCP server: handshake + list its tools.

Used to validate a Connector before saving it (and on re-probe when its URL /
auth changes). Talks the MCP Streamable HTTP transport via the official ``mcp``
SDK directly — the backend does not depend on axon. All failures collapse to
:class:`ConnectorUnreachableError` so the endpoint layer needs no translation.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field

from mcp import ClientSession
from mcp.types import Implementation, Tool
from mcp.client.streamable_http import streamablehttp_client

from app.errors import ConnectorUnreachableError
from app.models.mcp_server import MCPServerAuthType
from app.schemas.mcp_server import MAX_DISABLED_TOOLS, MAX_TOOL_NAME_LEN
from app.services.mcp.url_guard import guard_public_url, guarded_client

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 20.0
_LIST_TIMEOUT = 20.0

# Ceiling on a whole probe, which the two above do not add up to. They bound the
# handshake and the listing; the DNS pre-flight has no bound of its own, and the
# SDK sends a session-terminating DELETE on the way *out* of its context — a
# server answering that one byte at a time keeps resetting httpx's per-read
# clock while an API request and its database session are held open.
_PROBE_TIMEOUT = 60.0
# Backstop against a server that paginates ``tools/list`` forever; real
# catalogues are far smaller (mirrors axon's ``_MAX_LIST_PAGES``).
_MAX_LIST_PAGES = 100
# Descriptions land in a JSONB column that ships with every connector list
# response and renders in a compact row. Long enough for a real paragraph, short
# enough that a hostile server can't make the listing endpoint expensive.
_MAX_DESCRIPTION_CHARS = 500


# An icon is rendered by the browser, so an oversize ``data:`` URI would bloat
# every connector-list response for no visual gain. Comfortably fits a real SVG.
_MAX_ICON_SRC_LEN = 4096

# How many of a server's icons to keep. Keeping more than one exists so the
# browser can choose by pixel density and colour scheme, which needs a handful —
# two themes times a few resolutions. Past that it is a server handing us a list
# to store and re-serialize on every request.
_MAX_ICONS = 8


@dataclass(frozen=True)
class ProbeResult:
    server_name: str
    tools: list[dict[str, str]]  # ``[{"name": ..., "description": ...}]``
    # Every usable icon the server advertises (SEP-973), not the best of them:
    # which is best depends on the viewer's pixel density and colour scheme, and
    # neither is knowable here. The browser chooses — see ``ConnectorIcon`` on
    # the client — so this side sanitizes and passes on.
    icons: list[dict] = field(default_factory=list)


def build_auth_headers(
    auth_type: MCPServerAuthType, credential: str | None,
) -> dict[str, str]:
    """Turn a Connector's auth config into HTTP headers.

    BEARER → ``Authorization: Bearer <token>``; HEADER → the stored
    ``{"header", "value"}`` pair; NONE (or no credential) → no headers.
    """
    if auth_type == MCPServerAuthType.NONE or not credential:
        return {}
    if auth_type == MCPServerAuthType.BEARER:
        return {"Authorization": f"Bearer {credential}"}
    if auth_type == MCPServerAuthType.HEADER:
        pair = json.loads(credential)
        return {pair["header"]: pair["value"]}
    return {}


async def _list_all_tools(session: ClientSession) -> list[dict[str, str]]:
    """Every tool the server exposes, following ``tools/list`` pagination.

    One page is not the catalog: the worker paginates, so stopping at page one
    would cache a subset the user can neither see nor switch off while the
    agent still gets the rest. Bounded by one deadline across all pages and by
    a page cap, so a server that paginates forever can't stall an add.

    Bounded by what the user can act on, too — this listing exists to be
    rendered and switched off, so an entry outside ``disabled_tools``' own
    limits would be dead weight in the column. That is a different question
    from axon's caps on the same catalog, which bound what the *model* is sent.
    """
    deadline = asyncio.get_running_loop().time() + _LIST_TIMEOUT
    tools: list[dict[str, str]] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    for _ in range(_MAX_LIST_PAGES):
        page = await asyncio.wait_for(
            session.list_tools(cursor=cursor),
            deadline - asyncio.get_running_loop().time(),
        )
        if _admit_page(tools, page.tools):
            return tools
        cursor = page.nextCursor
        if not cursor:
            return tools
        if cursor in seen_cursors:
            raise ConnectorUnreachableError(
                f"The MCP server repeated its tools/list cursor after "
                f"{len(tools)} tools, so its catalog cannot be read whole."
            )
        seen_cursors.add(cursor)
    raise ConnectorUnreachableError(
        f"The MCP server did not finish listing its tools within "
        f"{_MAX_LIST_PAGES} pages."
    )


def _admit_page(tools: list[dict[str, str]], page: list[Tool]) -> bool:
    """Append the switchable tools of one page; ``True`` once the listing is full.

    A name past ``MAX_TOOL_NAME_LEN`` can never appear in ``disabled_tools`` —
    the schema rejects it — so storing it would show the user a switch that
    cannot be thrown. Descriptions are truncated rather than dropped: unlike
    axon, which needs a whole schema or none, this copy only has to read.
    """
    for tool in page:
        if len(tools) >= MAX_DISABLED_TOOLS:
            logger.warning(
                "MCP server advertises more than %d tools; the listing stops "
                "there, since no more could be switched off",
                MAX_DISABLED_TOOLS,
            )
            return True
        if not tool.name or len(tool.name) > MAX_TOOL_NAME_LEN:
            # Logged with a prefix, not whole — the name is why it was dropped.
            # Empty counts: ``disabled_tools`` requires at least one character,
            # so a nameless tool would render as a blank row the user cannot
            # switch off, and the PATCH to try would 422.
            logger.warning(
                "Dropping MCP tool %r...: its %d-character name is outside what "
                "``disabled_tools`` accepts, so it could never be switched off",
                tool.name[:64], len(tool.name),
            )
            continue
        tools.append({
            "name": tool.name,
            "description": (tool.description or "")[:_MAX_DESCRIPTION_CHARS],
        })
    return False


async def probe(
    url: str, auth_type: MCPServerAuthType, credential: str | None,
) -> ProbeResult:
    """Connect to *url*, complete the MCP handshake, and list its tools.

    Raises :class:`InvalidConnectorUrlError` (via the SSRF guard) for an unsafe
    URL, or :class:`ConnectorUnreachableError` for any handshake failure — the
    deadline included, since a server that never finishes is unreachable in
    every way that matters to the caller.
    """
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT):
            return await _probe(url, auth_type, credential)
    except TimeoutError as exc:
        raise ConnectorUnreachableError(
            "The MCP server took too long to answer."
        ) from exc


async def _probe(
    url: str, auth_type: MCPServerAuthType, credential: str | None,
) -> ProbeResult:
    """The probe itself, run under its caller's deadline."""
    # Raises InvalidConnectorUrlError — deliberately outside the try below so it
    # isn't collapsed into ConnectorUnreachableError.
    await guard_public_url(url)
    headers = build_auth_headers(auth_type, credential)

    try:
        async with streamablehttp_client(
            url,
            headers=headers,
            timeout=_CONNECT_TIMEOUT,
            sse_read_timeout=_LIST_TIMEOUT,
            # DNS-pin + no-redirect client, so the handshake can't be rebound or
            # 3xx-bounced to an internal host after the pre-flight guard above.
            httpx_client_factory=guarded_client,
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                initialize_result = await asyncio.wait_for(
                    session.initialize(), _CONNECT_TIMEOUT
                )
                tools = await _list_all_tools(session)
    except Exception as exc:
        raise ConnectorUnreachableError(
            f"Could not reach the MCP server: {exc}"
        ) from exc

    server_info = initialize_result.serverInfo
    server_name = server_info.name if server_info else url
    return ProbeResult(
        server_name=server_name,
        tools=tools,
        icons=_usable_icons(server_info),
    )


def _usable_icons(server_info: Implementation | None) -> list[dict]:
    """The server's icons (SEP-973), reduced to the ones we can safely render.

    All of them, not the first that looks usable. Choosing is the browser's job:
    it knows the device's pixel density and the user's colour scheme, and has
    ``srcset`` and ``prefers-color-scheme`` to act on them. Nothing on this side
    knows either, so picking here can only be a guess that overrides a decision
    the browser would have made correctly.

    Filtered rather than forwarded. ``src`` must be an https URL or an inline
    ``data:image/`` URI — rejecting other schemes is a MUST in the spec, and
    ``javascript:`` is why. Length and count are bounded because this list is
    stored and re-serialized into every connector response. Fields are copied by
    name, so a server cannot park arbitrary JSON in our column.

    No SSRF concern: the browser fetches these, this server never does. It does
    mean a cross-origin ``src`` lets that host see the viewer's IP. The spec
    would rather clients required same-origin, but hosting icons on a CDN is
    both common and encouraged for reachability, so the cost is taken instead
    and the fetch carries no credentials or referrer (see ``ConnectorIcon``).
    """
    if server_info is None:
        return []
    usable: list[dict] = []
    for icon in server_info.icons or []:
        if len(usable) == _MAX_ICONS:
            break
        src = icon.src.strip()
        if len(src) > _MAX_ICON_SRC_LEN:
            continue
        if not (src.startswith("https://") or src.startswith("data:image/")):
            continue
        usable.append({
            "src": src,
            "mime_type": icon.mimeType,
            "sizes": icon.sizes,
            # Not modelled by the SDK yet, so it arrives as an extra field.
            # Worth carrying: it is what lets the client offer a dark variant.
            "theme": (icon.model_extra or {}).get("theme"),
        })
    return usable
