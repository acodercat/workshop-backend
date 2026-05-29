"""Shared constants for the backend."""

from enum import StrEnum


class EventType(StrEnum):
    """Wire-format event types on the per-session SSE stream.

    Two categories share one enum because both end up as ``event:``
    lines on the same channel:

    - ``session.*`` — transport-layer handshakes synthesised by the
      SSE generator (``app.services.session.streaming``) itself.
      Never written to the Redis ``session_stream:{id}``; the worker
      doesn't know about them.
    - ``agent.*`` — produced by the worker and XADD'd to Redis.
      Values must match the strings emitted by
      ``agent_worker.wire.session_events.SessionEventEmitter``. The
      backend keeps its own enum so it has no runtime dependency on
      ``axon`` — it only consumes the stream.
    """
    SESSION_READY = "session.ready"
    # Marks the Phase-1-replay → Phase-2-live boundary so clients can
    # tell "I've caught up to live" without time-based heuristics.
    SESSION_LIVE = "session.live"
    AGENT_MESSAGE_CHUNK = "agent.message_chunk"
    AGENT_THINKING_CHUNK = "agent.thinking_chunk"
    AGENT_THINKING = "agent.thinking"
    AGENT_MESSAGE = "agent.message"
    AGENT_TOOL_USE_STARTED = "agent.tool_use_started"
    AGENT_TOOL_USE_ARGS_CHUNK = "agent.tool_use_args_chunk"
    AGENT_TOOL_USE = "agent.tool_use"
    AGENT_TOOL_PROGRESS = "agent.tool_progress"
    AGENT_TOOL_RESULT = "agent.tool_result"
    AGENT_FILE_OUTPUT = "agent.file_output"
    AGENT_STATUS = "agent.status"
    AGENT_ERROR = "agent.error"
    AGENT_STOPPED = "agent.stopped"
