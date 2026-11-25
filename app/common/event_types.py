"""Shared constants for the backend."""

from enum import StrEnum


class EventType(StrEnum):
    """Wire-format event types on the per-session SSE stream.

    Four categories share one enum because all end up as ``event:``
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
    - ``sandbox.*`` — sandbox-lifecycle handshakes the worker synthesises
      outside the agent loop (``SessionEventEmitter.emit_sandbox``) and
      XADDs to Redis, so the UI can show a "starting sandbox" state. Like
      ``agent.*`` they ride the stream; like ``session.*`` they aren't
      axon events.
    - ``user.*`` — produced by the worker like ``agent.*``: what the user
      said that reached a running turn. ``user.steering`` is a queued
      message the turn took (``queued_message.steer_turn_id``).

    The backend forwards stream entries verbatim and only matches on
    ``SESSION_*`` / ``AGENT_STOPPED`` here, so this enum is the documented
    catalog of what crosses the wire, not a filter.
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
    AGENT_TOOL_USE = "agent.tool_use"
    AGENT_TOOL_PROGRESS = "agent.tool_progress"
    AGENT_TOOL_RESULT = "agent.tool_result"
    AGENT_TODO_UPDATE = "agent.todo_update"
    AGENT_STATUS = "agent.status"
    AGENT_ERROR = "agent.error"
    AGENT_STOPPED = "agent.stopped"
    USER_STEERING = "user.steering"
    # Sandbox-lifecycle handshakes the worker synthesises outside the agent
    # loop (``emit_sandbox``); ``creating`` → ``created`` brackets the
    # "starting sandbox" UI state.
    SANDBOX_CREATING = "sandbox.creating"
    SANDBOX_CREATED = "sandbox.created"
