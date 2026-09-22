"""SSE event helpers + the discriminated union of wire events.

The worker writes typed events to ``session_stream:{session_id}`` as
Redis stream entries. Every event shares two headers — ``type`` (the
wire event type, see :class:`~app.common.event_types.EventType`) and
``ts`` (a stringified float, seconds since epoch). The remaining
fields are event-type-specific, declared per variant below.

All values are strings because Redis Streams store flat ``str → str``
maps. Non-string values the worker writes (dicts, lists, ints,
floats) go through the worker's ``_encode_fields``
(``agent_worker.infra.redis_queue``) which JSON-encodes non-strings —
so e.g. ``elapsed`` is the literal string ``"2.5"`` and structured
fields like ``args`` / ``files`` / ``usage`` are JSON strings the
backend forwards verbatim to the frontend. ``None``-valued fields are
dropped entirely by ``_encode_fields`` (they never reach the wire), so
the consumer treats an absent field and a null one the same.

The authoritative producer of these shapes is
``agent_worker/wire/session_events.py``; if you need to reason about
exactly what the worker writes, read that. This file documents the
consumer-side view.
"""

from typing import Literal, NotRequired, TypedDict

import orjson


class _EventBase(TypedDict):
    """Fields every wire event has."""

    ts: str


class SessionReadyEvent(_EventBase):
    """``session.ready`` — backend handshake, the first SSE message
    after stream open. Header-only; per-connection (fires on every
    reconnect); never persisted to Redis. Lets the client distinguish
    "connected, waiting for worker" from "connection broken".
    """

    type: Literal["session.ready"]


class AgentMessageChunkEvent(_EventBase):
    """``agent.message_chunk`` — one streamed text chunk of the agent's reply."""

    type: Literal["agent.message_chunk"]
    content: str


class AgentThinkingChunkEvent(_EventBase):
    """``agent.thinking_chunk`` — one streamed chunk of the model's reasoning.

    Emitted by thinking-capable providers (OpenAI o-series, Anthropic
    Claude with extended thinking, Google Gemini thought parts,
    Moonshot Kimi / Qwen QwQ's ``reasoning_content``). Rendered as a
    separate UI channel from the final answer — typically a
    collapsible "reasoning" region.
    """

    type: Literal["agent.thinking_chunk"]
    content: str


class AgentThinkingEvent(_EventBase):
    """``agent.thinking`` — a completed thinking segment.

    ``duration_ms`` is authoritative: measured at the source (axon) as the
    span from the first reasoning token to the moment reasoning stops. The
    frontend displays it directly instead of diffing event timestamps,
    which over-counts across recovery gaps / stalls / tool execution.
    """

    type: Literal["agent.thinking"]
    duration_ms: int


class AgentMessageEvent(_EventBase):
    """``agent.message`` — the complete agent message after streaming ends.

    ``stop_reason`` is the *provider-native* finish reason
    (``"stop"``/``"length"``/``"tool_calls"``/...), NOT the agent-level
    :class:`~axon.events.StopReason` (which appears on ``agent.stopped``).
    ``usage`` is a JSON-encoded ``TokenUsage`` dict or ``"null"``.
    """

    type: Literal["agent.message"]
    content: str
    stop_reason: str
    usage: NotRequired[str]


class AgentToolUseStartedEvent(_EventBase):
    """``agent.tool_use_started`` — the model has identified the tool
    but its arguments are still streaming. Header-only; the matching
    ``agent.tool_use`` later carries the full payload and is paired by
    ``tool_use_id``. Orphan-allowed: a truncated stream may not
    produce a matching ``agent.tool_use``.
    """

    type: Literal["agent.tool_use_started"]
    tool: str
    tool_use_id: str


class AgentToolUseArgsChunkEvent(_EventBase):
    """``agent.tool_use_args_chunk`` — one fragment of the tool call's
    arguments JSON. Concatenating every ``delta`` for one
    ``tool_use_id`` reconstructs the final args JSON.

    The fragment is opaque text — partial JSON at chunk boundaries
    (``'{"command":"ec'``) does not parse on its own. Same
    orphan-allowed contract as ``agent.tool_use_started``.
    """

    type: Literal["agent.tool_use_args_chunk"]
    tool_use_id: str
    delta: str


class AgentToolUseEvent(_EventBase):
    """``agent.tool_use`` — the model is invoking a tool.

    ``tool_use_id`` is the emitter's rename of axon's ``id`` — the
    frontend pairs ``tool_use`` with its matching ``tool_result`` on it.
    ``args`` is a JSON-encoded dict (possibly empty ``{}``).

    ``reason`` is the LLM's brief UI caption for the call, surfaced as
    the node summary in the UI. ``NotRequired`` because the LLM may skip
    the optional axon field, and pre-rollout sessions in Redis won't
    carry it.
    """

    type: Literal["agent.tool_use"]
    tool: str
    tool_use_id: str
    args: str
    reason: NotRequired[str]


class AgentToolProgressEvent(_EventBase):
    """``agent.tool_progress`` — periodic update during tool execution.

    ``elapsed`` is a stringified float (seconds). ``output`` is the
    tool's partial stdout/stderr so far, or ``"null"`` if unavailable.
    """

    type: Literal["agent.tool_progress"]
    tool: str
    tool_use_id: str
    elapsed: str
    output: NotRequired[str]


class AgentToolResultEvent(_EventBase):
    """``agent.tool_result`` — a tool has finished.

    ``output`` is the textual result (already a string in axon's
    ``ToolResult.output``). ``files`` is a JSON-encoded list of
    :class:`~agent_worker.storage.FileAsset` dicts; empty list if the
    tool produced no files. ``is_error`` mirrors axon's
    ``ToolResult.is_error`` discriminator; absent / ``"false"`` on
    success, ``"true"`` when the tool errored (``output`` then carries
    the error message). The worker omits the field on success to keep
    payloads lean.
    """

    type: Literal["agent.tool_result"]
    tool: str
    tool_use_id: str
    output: str
    files: str
    is_error: NotRequired[str]


class AgentStatusEvent(_EventBase):
    """``agent.status`` — agent-loop lifecycle signal.

    Two producers, different field sets:

    - Synthetic ``emitter.emit_status(s)`` (runner-side lifecycle hooks
      like ``"started"``) — only ``status`` is present.
    - Real ``AgentStatusEvent`` from axon (compaction, output recovery) —
      all of ``status`` / ``before`` / ``after`` / ``recovery`` /
      ``max_recoveries`` are written by ``dataclasses.asdict``; the
      optional ``int | None`` fields land on the wire as the literal
      string ``"null"`` when unset.
    """

    type: Literal["agent.status"]
    status: str
    before: NotRequired[str]
    after: NotRequired[str]
    recovery: NotRequired[str]
    max_recoveries: NotRequired[str]


class AgentErrorEvent(_EventBase):
    """``agent.error`` — a non-fatal, user-safe error message.

    ``content`` only. Both producers (axon's ``AgentErrorEvent`` and the
    runner's ``emit_error(...)``) deliberately keep all detail — exception
    class, upstream body, stack trace — in the worker log, never on the
    wire, so a browser-visible event can't leak internals.
    """

    type: Literal["agent.error"]
    content: str


class AgentStoppedEvent(_EventBase):
    """``agent.stopped`` — always the terminal event of a session.

    Two producers, different shapes:

    - Real ``AgentStoppedEvent`` from axon — ``content`` + ``turns`` +
      ``elapsed`` + ``stop_reason`` + ``usage`` + ``total_model_latency``.
    - Synthetic ``emitter.emit_terminal_done()`` (error-path fallback
      when the agent loop died before emitting its own stopped event) —
      only ``content`` (typically empty string).

    ``termination_reason`` is the worker's persisted why-it-ended
    (mirrors ``session_turn.termination_reason``; values from
    :class:`app.models.session_turn.TerminationReason`). Orthogonal to
    ``stop_reason`` (axon's agent-level finish reason): a budget-enforcer
    stop and a user stop both carry ``stop_reason="cancelled"`` but
    differ here (``insufficient_credits`` vs ``user_cancelled``), which
    is what lets the frontend pick the right copy. Absent on
    pre-rollout events.

    Everything except ``content`` is ``NotRequired``.
    """

    type: Literal["agent.stopped"]
    content: str
    turns: NotRequired[str]
    elapsed: NotRequired[str]
    stop_reason: NotRequired[str]
    termination_reason: NotRequired[str]
    usage: NotRequired[str]
    total_model_latency: NotRequired[str]


#: Discriminated union of every event sent over the per-session SSE stream.
#:
#: Most variants are produced by the worker (``agent.*`` + ``user.*``) and
#: replayed from Redis. ``session.ready`` is produced by the backend SSE
#: forwarder itself on every (re)connect. Narrow with
#: ``match event["type"]:`` / ``if event["type"] == "...":``. Most
#: backend code doesn't need to narrow — :func:`event_to_sse` just
#: forwards non-header fields to the frontend as JSON.
StreamEvent = (
    SessionReadyEvent
    | AgentMessageChunkEvent
    | AgentThinkingChunkEvent
    | AgentThinkingEvent
    | AgentMessageEvent
    | AgentToolUseStartedEvent
    | AgentToolUseArgsChunkEvent
    | AgentToolUseEvent
    | AgentToolProgressEvent
    | AgentToolResultEvent
    | AgentStatusEvent
    | AgentErrorEvent
    | AgentStoppedEvent
)


# Header fields stripped from the per-event ``data`` payload sent over SSE.
# ``type`` is redundant with the SSE ``event:`` line. ``turn_id`` is a
# backend-only routing stamp — the SSE generator uses it to drop a zombie
# worker's stale-turn events (see ``streaming._is_stale_entry``); it must
# not leak to the frontend. ``ts`` stays on the wire — it's the worker's
# emit-time clock (see ``agent_worker/wire/session_events.py``) and the
# frontend uses it for elapsed-time displays. Keeping it here instead
# of deriving from the SSE ``id:`` field avoids coupling the frontend to
# the Redis Streams entry-id format.
_INTERNAL_FIELDS = frozenset({"type", "turn_id"})


class SSEMessage(TypedDict, total=False):
    """Dict accepted by sse-starlette's EventSourceResponse."""

    event: str
    data: str
    id: str


def sse_event(
    event_type: str,
    data: str | None = None,
    event_id: str | None = None,
) -> SSEMessage:
    """Build an SSE event dict.

    ``event_type`` maps to the SSE spec's ``event:`` field — the string
    the frontend's ``EventSource.addEventListener(...)`` binds to.
    ``data`` and ``event_id`` are omitted from the result entirely when
    ``None`` (distinct from an empty-string value, which is preserved).
    """
    msg: SSEMessage = {"event": event_type}
    if data is not None:
        msg["data"] = data
    if event_id is not None:
        msg["id"] = event_id
    return msg


def event_to_sse(event: StreamEvent, entry_id: str) -> SSEMessage:
    """Convert a Redis Stream event to an SSE event dict.

    ``entry_id`` is the Redis-assigned stream entry id (monotonic,
    shaped ``"{ms}-{seq}"``) — used as the SSE event's ``id:`` field so
    clients can resume from a known position via ``Last-Event-ID``.
    """
    event_type = event["type"]
    fields = {k: v for k, v in event.items() if k not in _INTERNAL_FIELDS}
    data = orjson.dumps(fields).decode() if fields else None
    return sse_event(event_type, data, entry_id)
