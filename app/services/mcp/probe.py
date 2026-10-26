"""Probe a remote MCP server: handshake + list its tools.

Used to validate a Connector before saving it (and on re-probe when its URL /
auth changes). Talks the MCP Streamable HTTP transport via the official ``mcp``
SDK directly — the backend does not depend on axon. Failures are raised as
:class:`ConnectorUnreachableError` or one of its subclasses, so the endpoint
layer needs no translation.
"""

import asyncio
import json
import logging
import ssl
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import INTERNAL_ERROR, METHOD_NOT_FOUND, Implementation, Tool

from app.errors import (
    ConnectorAuthRejectedError,
    ConnectorHttpStatusError,
    ConnectorNotMcpEndpointError,
    ConnectorTlsFailedError,
    ConnectorUnreachableError,
)
from app.models.mcp_server import MCPServerAuthType
from app.schemas.mcp_server import MAX_DISABLED_TOOLS, MAX_TOOL_NAME_LEN
from app.services.mcp.url_guard import guard_public_url, guarded_client

logger = logging.getLogger(__name__)

# Per JSON-RPC request: the handshake — which ``mode="auto"`` may spend twice —
# and each ``tools/list`` page get this much each.
_CONNECT_TIMEOUT = 20.0
# Per read on the wire. The successor of two v1 knobs — the transport's
# ``sse_read_timeout`` and a deadline shared across list pages — folded into
# the one httpx2 client everything now rides on.
_READ_TIMEOUT = 20.0

# Ceiling on a whole probe, which the two above do not add up to: they bound
# each request and each read, not how many of either a server can demand. The
# DNS pre-flight has no bound of its own, and the SDK sends a
# session-terminating DELETE on the way *out* of its context — a server
# answering that one byte at a time keeps resetting httpx2's per-read clock
# while an API request and its database session are held open.
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


# The SDK's wording when a response body will not parse as JSON-RPC. Matched to
# *add* a sentence, never to decide anything: if a future SDK renames it, the
# hint disappears and the message stays correct.
_NOT_JSONRPC = "failed to parse"

# The SDK's wording when a response arrives with a content type that is neither
# JSON nor an event stream — an HTML page, which is what a web root serves to a
# URL that forgot its ``/mcp``. Same discipline as ``_NOT_JSONRPC``: matched to
# refine the error, never to decide anything. A rename costs the refinement and
# leaves the generic error correct.
_NOT_MCP_CONTENT_TYPE = "unexpected content type"

# What that failure almost always means here. A server that answers HTTP but
# not JSON-RPC is usually the wrong path: ``/sse`` belongs to MCP's legacy
# HTTP+SSE transport, which this client does not speak — it speaks Streamable
# HTTP, conventionally served at ``/mcp``.
_WRONG_ENDPOINT_HINT = (
    "The server answered, but not with MCP. Check the URL points at its "
    "Streamable HTTP endpoint — commonly one ending in /mcp. The legacy /sse "
    "transport is not supported."
)


@dataclass(frozen=True)
class ProbeResult:
    server_name: str
    tools: list[dict[str, str]]  # ``[{"name": ..., "description": ...}]``
    # Every usable icon the server advertises (SEP-973), not the best of them:
    # which is best depends on the viewer's pixel density and colour scheme, and
    # neither is knowable here. The browser chooses — see ``ConnectorIcon`` on
    # the client — so this side sanitizes and passes on.
    icons: list[dict] = field(default_factory=list)
    # ``serverInfo.description`` (MCP): "a human-readable description of what this
    # implementation does". What the UI shows for a custom Connector, which has no
    # curated catalog copy. Not ``instructions`` — that is LLM guidance, not a
    # display string. ``None`` when the server advertised none.
    description: str | None = None


def build_auth_headers(
    auth_type: MCPServerAuthType,
    credential: str | None,
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


async def _list_all_tools(session: Client) -> list[dict[str, str]]:
    """Every tool the server exposes, following ``tools/list`` pagination.

    One page is not the catalog: the worker paginates, so stopping at page one
    would cache a subset the user can neither see nor switch off while the
    agent still gets the rest. A page cap stops a server that paginates
    forever; each page's request is bounded by ``Client(read_timeout_seconds)``
    and the whole probe by ``_PROBE_TIMEOUT``. Nothing is shared across pages —
    a single deadline used to be, and page ten inherited whatever page one had
    left.

    Bounded by what the user can act on, too — this listing exists to be
    rendered and switched off, so an entry outside ``disabled_tools``' own
    limits would be dead weight in the column. That is a different question
    from axon's caps on the same catalog, which bound what the *model* is sent.
    """
    tools: list[dict[str, str]] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    for _ in range(_MAX_LIST_PAGES):
        page = await session.list_tools(cursor=cursor)
        if _admit_page(tools, page.tools):
            return tools
        cursor = page.next_cursor
        if not cursor:
            return tools
        if cursor in seen_cursors:
            raise ConnectorUnreachableError(
                f"The MCP server repeated its tools/list cursor after "
                f"{len(tools)} tools, so its catalog cannot be read whole."
            )
        seen_cursors.add(cursor)
    raise ConnectorUnreachableError(
        f"The MCP server did not finish listing its tools within {_MAX_LIST_PAGES} pages."
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
                tool.name[:64],
                len(tool.name),
            )
            continue
        tools.append(
            {
                "name": tool.name,
                "description": (tool.description or "")[:_MAX_DESCRIPTION_CHARS],
            }
        )
    return False


async def probe(
    url: str,
    auth_type: MCPServerAuthType,
    credential: str | None,
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
        # Logged here rather than in ``_unreachable``: the whole-probe deadline
        # fires outside the try that feeds it, and a timeout that left no trace
        # in the log is the same dead end as the failures that one exists to
        # record.
        logger.warning(
            "MCP probe failed (%s): probe deadline exceeded",
            ConnectorUnreachableError.error_type,
        )
        raise ConnectorUnreachableError("The MCP server took too long to answer.") from exc


async def _probe(
    url: str,
    auth_type: MCPServerAuthType,
    credential: str | None,
) -> ProbeResult:
    """The probe itself, run under its caller's deadline."""
    # Raises InvalidConnectorUrlError — deliberately outside the try below so it
    # isn't collapsed into ConnectorUnreachableError.
    await guard_public_url(url)
    headers = build_auth_headers(auth_type, credential)

    # Set once the session is open, which is what separates "this URL is not
    # an MCP endpoint" from "this MCP server went wrong later" — see
    # ``_unreachable``.
    handshook = False

    # The refused statuses, recorded as they go past.
    #
    # The SDK does not leave them anywhere else: a non-2xx POST becomes a
    # JSON-RPC error carrying ``INTERNAL_ERROR`` and the sentence "Server
    # returned an error response", with the status dropped — so 401 and 500
    # reach us identically, and "check your credential" and "the server is
    # broken" are not the same advice. Only ``raise_for_status`` on the SSE
    # paths preserves one, which the handshake does not take.
    refused: list[int] = []

    async def _record_refusal(response: httpx2.Response) -> None:
        if response.status_code >= 400:
            refused.append(response.status_code)

    try:
        # The client is entered *outside* the transport, and the order is not
        # cosmetic: the SDK does not close what it did not open, and it sends a
        # session-terminating DELETE on its way out. Closing ours first would
        # fail that teardown against every server that honours termination.
        async with guarded_client(
            headers=headers,
            # One object where v1 took two arguments: the transport no longer
            # has an ``sse_read_timeout`` of its own, so the connect bound and
            # the stream-read bound travel together here.
            timeout=httpx2.Timeout(_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
        ) as http_client:
            # Hooked here rather than in ``guarded_client``: that helper is
            # shared with the OAuth flow, which has no use for this and should
            # not grow a parameter for it.
            http_client.event_hooks["response"].append(_record_refusal)
            # The transport is handed over unentered: ``Client`` takes an async
            # context manager yielding the stream pair and enters it itself.
            async with Client(
                streamable_http_client(url, http_client=http_client),
                # Probes ``server/discover`` and falls back to ``initialize``,
                # which is the point of the SDK bump: a server speaking only the
                # 2026-07-28 stateless protocol refuses ``initialize`` outright,
                # and used to be unaddable for it.
                mode="auto",
                # Bounds each JSON-RPC request rather than the whole handshake —
                # ``mode="auto"`` may send two, and one shared deadline around
                # both would halve what each gets.
                read_timeout_seconds=_CONNECT_TIMEOUT,
                # A probe exists to answer "what is there *now*". Each one builds
                # its own client, so the SDK's per-client cache could never serve
                # a stale answer anyway — but off is one less thing between the
                # user's refresh button and the server.
                cache=None,
            ) as session:
                handshook = True
                # Asking a server that never advertised ``tools`` produces
                # ``-32601``, which this function would report as the server
                # being unreachable rather than as the absence it is.
                tools = (
                    await _list_all_tools(session)
                    if session.server_capabilities.tools is not None
                    else []
                )
                server_info = session.server_info
    except Exception as exc:
        raise _unreachable(exc, handshook=handshook, refused=refused) from exc

    # ``None`` where v1 always had a value: a 2026-era server stamps its identity
    # optionally, so an anonymous one is named after its URL and shows no
    # artwork. Its tools still arrive, and the name is the user's to edit.
    server_name = server_info.name if server_info else url
    # ``description`` is optional in ``Implementation``; read it defensively so a
    # server (or SDK shape) without one is not an error.
    raw_description = getattr(server_info, "description", None)
    description = (raw_description or "").strip()[:_MAX_DESCRIPTION_CHARS] or None
    return ProbeResult(
        server_name=server_name,
        tools=tools,
        icons=_usable_icons(server_info),
        description=description,
    )


def _leaves(exc: BaseException) -> Iterator[BaseException]:
    """Every non-group exception reachable from *exc*, nearest first.

    Groups are flattened and ``__cause__`` / ``__context__`` followed, because
    the one that knows what went wrong can be at any of those — including a
    group's own: raising a group from ``__aexit__`` while another exception is
    in flight leaves that one in the group's ``__context__``. Cycles are
    possible — an exception can be its own context after a re-raise — so each
    is yielded once.

    Breadth-first, and that is the property worth keeping: siblings before
    contexts, so an unrelated exception that merely happened to be in flight is
    consulted only after every direct member has had nothing to say.
    """
    seen: set[int] = set()
    queue: deque[BaseException] = deque([exc])
    while queue:
        current = queue.popleft()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            queue.extend(current.exceptions)
        else:
            yield current
        queue.extend(e for e in (current.__cause__, current.__context__) if e is not None)


def _describe(exc: BaseException) -> str:
    """The failure in the words of whatever actually failed.

    anyio runs the SDK's transport in a task group, so everything raised inside
    it reaches us wrapped: ``str`` of an ``ExceptionGroup`` is "unhandled errors
    in a TaskGroup (1 sub-exception)" — the same sentence for a rejected
    credential, a timeout, and a server that is not MCP at all, which is what
    made a real report unreadable. The leaves carry the message; this puts one
    of them back.

    First line only. A pydantic ``ValidationError`` arrives here as eleven
    stanzas naming every member of the JSON-RPC union, and none of them tells a
    user anything the first line does not.

    A leaf with nothing to say falls back to its type name rather than to the
    group. anyio's stream exceptions — ``EndOfStream``, ``ClosedResourceError``,
    ``BrokenResourceError``, all of which this transport traffics in — stringify
    to ``""``, and returning ``str(exc)`` for them put the TaskGroup sentence
    back, which is the whole thing this function exists to remove.
    """
    nearest: BaseException | None = None
    for leaf in _leaves(exc):
        nearest = nearest or leaf
        first_line = str(leaf).split("\n", 1)[0].strip()
        if first_line:
            return first_line
    return type(nearest or exc).__name__


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status of the nearest leaf that carries a response, if any.

    Duck-typed on ``leaf.response.status_code`` rather than matched against
    ``httpx2.HTTPStatusError``: the SDK wraps transport failures in its own
    classes, and what matters is that something answered with a status, not
    which library minted the object holding it.
    """
    for leaf in _leaves(exc):
        status_code = getattr(getattr(leaf, "response", None), "status_code", None)
        if isinstance(status_code, int):
            return status_code
    return None


def _unreachable(
    exc: BaseException, *, handshook: bool, refused: list[int] | None = None
) -> ConnectorUnreachableError:
    """Turn a probe failure into the error the user is shown.

    The type is chosen by **what the user would do next**, not by which library
    raised: a rejected credential and a refused connection are one sentence
    apart in English and a world apart in remedy. Anything that does not sort
    into one of those stays :class:`ConnectorUnreachableError`, whose wording
    covers the group it is left with — timed out, DNS, connection refused.

    ``handshook`` is what makes the wrong-endpoint hint safe to give. Before the
    session opens, a body that will not parse as JSON-RPC means the URL is
    probably not an MCP endpoint at all. After it opens, the endpoint has
    already proved it speaks Streamable HTTP — a parse failure while paging
    ``tools/list`` is the server misbehaving mid-catalogue, and telling the user
    to change a URL that is right would send them the wrong way.

    Every branch logs ``detail`` before returning. The client is shown a
    localized sentence built from the error *type* and never this string, so
    without the log line the only description of what actually happened is
    discarded at the browser — which is exactly how "it says it cannot connect"
    became unanswerable. The log keeps it, untruncated, under the request id
    ``RequestIDMiddleware`` bound.

    The distilled line, and deliberately **not** ``exc_info``. The traceback
    here is the anyio TaskGroup wrapper plus, for a non-MCP answer, a pydantic
    union error naming all eleven JSON-RPC members — the noise ``_describe``
    exists to strip. This is triggered by anyone mistyping a URL, so it is
    unbounded in frequency against a 10 MB rotating sink, and none of what it
    would add describes the user's server.
    """
    detail = _describe(exc)
    # Already the sentence to show: ``_list_all_tools`` raises these for a
    # repeated cursor or a runaway page count, from inside the SDK's task group,
    # so they arrive wrapped like everything else — and classified afresh they
    # would read as a server that could not be reached, when it was reached,
    # handshook and paginated.
    own = next((leaf for leaf in _leaves(exc) if isinstance(leaf, ConnectorUnreachableError)), None)
    if own is not None:
        error = own
    else:
        error = _classify(exc, handshook=handshook, detail=detail, refused=refused or [])
    logger.warning("MCP probe failed (%s): %s", error.error_type, detail)
    return error


def _first_mcp_error(exc: BaseException) -> MCPError | None:
    """The MCP-level error the session raised, if the failure is one.

    **The common shape, not an edge case.** For a server that answers but is
    not MCP, the transport raises nothing: it turns the answer into a JSON-RPC
    error and sends it up the stream, and the session raises *that*. So the
    signal never reaches the HTTP-exception checks above — a URL missing its
    ``/mcp`` suffix arrived as an unclassified failure until this looked for
    it here.
    """
    for leaf in _leaves(exc):
        if isinstance(leaf, MCPError):
            return leaf
    return None


def _classify(
    exc: BaseException, *, handshook: bool, detail: str, refused: list[int]
) -> ConnectorUnreachableError:
    """The branch of :func:`_unreachable` that picks the type."""
    if any(isinstance(leaf, ssl.SSLError) for leaf in _leaves(exc)):
        return ConnectorTlsFailedError(f"TLS verification failed: {detail}")

    # Before the SDK's stand-in, which cannot tell these apart: a credential the
    # server refused is the one failure here fixed by editing a key rather than
    # an address.
    if any(status_code in (401, 403) for status_code in refused):
        return ConnectorAuthRejectedError(
            f"The MCP server rejected the credential ({refused[-1]})."
        )

    # Before the session opens, an MCP-level error is the remote end telling us
    # what it is. Only before: afterwards the endpoint has proved itself, and
    # the same codes mean a server misbehaving mid-catalogue.
    mcp_error = _first_mcp_error(exc) if not handshook else None
    if mcp_error is not None:
        # ``METHOD_NOT_FOUND`` is the SDK's reading of a 404 with no session —
        # a gateway or a plain web root that has never heard of this method,
        # which is what a URL without its ``/mcp`` suffix looks like from here.
        # Matched on the code rather than its wording: the code is the
        # protocol's, the wording is this SDK's.
        if mcp_error.code == METHOD_NOT_FOUND or mcp_error.message.lower().startswith(
            _NOT_MCP_CONTENT_TYPE
        ):
            return ConnectorNotMcpEndpointError(f"{_WRONG_ENDPOINT_HINT} (detail: {detail})")
        if mcp_error.code == INTERNAL_ERROR:
            return ConnectorHttpStatusError("The MCP server answered with an error response.")

    status_code = _http_status(exc)
    if status_code in (401, 403):
        return ConnectorAuthRejectedError(
            f"The MCP server rejected the credential ({status_code})."
        )
    if status_code is not None and status_code >= 400:
        return ConnectorHttpStatusError(f"The MCP server answered HTTP {status_code}.")

    if not handshook and _NOT_JSONRPC in detail.lower():
        return ConnectorNotMcpEndpointError(f"{_WRONG_ENDPOINT_HINT} (detail: {detail})")
    return ConnectorUnreachableError(f"Could not reach the MCP server: {detail}")


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

    One thing this function can no longer soften: ``theme`` is now typed
    ``Literal["light", "dark"]``, and it is validated when the *handshake*
    result is parsed — long before anything here runs. A server advertising
    ``theme: "auto"`` therefore fails to connect at all rather than losing one
    icon. SEP-973 defines only those two values, so such a server is out of
    spec; under the 1.x models it was merely carried through as an extra.
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
        usable.append(
            {
                "src": src,
                "mime_type": icon.mime_type,
                "sizes": icon.sizes,
                # Modelled by the SDK since 2.x, where it used to arrive as an
                # extra field read off ``model_extra``. Worth carrying: it is
                # what lets the client offer a dark variant.
                "theme": icon.theme,
            }
        )
    return usable
