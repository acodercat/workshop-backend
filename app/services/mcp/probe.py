"""Probe a remote MCP server: handshake + list its tools.

Used to validate a Connector before saving it (and on re-probe when its URL /
auth changes). Talks the MCP Streamable HTTP transport via the official ``mcp``
SDK directly — the backend does not depend on axon. All failures collapse to
:class:`ConnectorUnreachableError` so the endpoint layer needs no translation.
"""

import asyncio
import json
from dataclasses import dataclass

from mcp import ClientSession
from mcp.types import Implementation
from mcp.client.streamable_http import streamablehttp_client

from app.errors import ConnectorUnreachableError
from app.models.mcp_server import MCPServerAuthType
from app.services.mcp.url_guard import guard_public_url, guarded_client

_CONNECT_TIMEOUT = 20.0
_LIST_TIMEOUT = 20.0


# An icon is rendered by the browser, so an oversize ``data:`` URI would bloat
# every connector-list response for no visual gain. Comfortably fits a real SVG.
_MAX_ICON_SRC_LEN = 4096


@dataclass(frozen=True)
class ProbeResult:
    server_name: str
    tools: list[dict[str, str]]  # ``[{"name": ..., "description": ...}]``
    icon_url: str | None = None  # from ``serverInfo.icons`` (SEP-973), if usable


def build_auth_headers(auth_type: MCPServerAuthType, credential: str | None) -> dict[str, str]:
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


async def probe(
    url: str, auth_type: MCPServerAuthType, credential: str | None,
) -> ProbeResult:
    """Connect to *url*, complete the MCP handshake, and list its tools.

    Raises :class:`InvalidConnectorUrlError` (via the SSRF guard) for an unsafe
    URL, or :class:`ConnectorUnreachableError` for any handshake failure.
    """
    # Raises InvalidConnectorUrlError — deliberately outside the try below so it
    # isn't collapsed into ConnectorUnreachableError.
    await guard_public_url(url)
    headers = build_auth_headers(auth_type, credential)

    try:
        async with streamablehttp_client(
            url, headers=headers, timeout=_CONNECT_TIMEOUT, sse_read_timeout=_LIST_TIMEOUT,
            # DNS-pin + no-redirect client, so the handshake can't be rebound or
            # 3xx-bounced to an internal host after the pre-flight guard above.
            httpx_client_factory=guarded_client,
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                initialize_result = await asyncio.wait_for(
                    session.initialize(), _CONNECT_TIMEOUT
                )
                list_result = await asyncio.wait_for(
                    session.list_tools(), _LIST_TIMEOUT
                )
    except Exception as exc:
        raise ConnectorUnreachableError(
            f"Could not reach the MCP server: {exc}"
        ) from exc

    server_info = initialize_result.serverInfo
    server_name = server_info.name if server_info else url
    tools = [
        {"name": tool.name, "description": tool.description or ""}
        for tool in list_result.tools
    ]
    return ProbeResult(
        server_name=server_name,
        tools=tools,
        icon_url=_usable_icon(server_info),
    )


def _usable_icon(server_info: Implementation | None) -> str | None:
    """The server's own icon (SEP-973), if it offers one we can safely render.

    Takes the first icon whose ``src`` the browser can load directly: an https
    URL or an inline ``data:image/`` URI. Anything else — other schemes, or a
    payload past :data:`_MAX_ICON_SRC_LEN` — is skipped, and the caller falls
    back to the built-in glyph. No SSRF concern: the browser fetches this, our
    server never does.
    """
    if server_info is None:
        return None
    for icon in server_info.icons or []:
        src = icon.src.strip()
        if len(src) > _MAX_ICON_SRC_LEN:
            continue
        if src.startswith("https://") or src.startswith("data:image/"):
            return src
    return None
