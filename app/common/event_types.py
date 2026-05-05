"""Shared constants for the backend."""

from enum import StrEnum


class EventType(StrEnum):
    """Wire-format event types on the worker → backend per-session stream
    (``session_stream:{session_id}``).

    Values must match the strings emitted by
    ``agent_worker.emitter.SessionEventEmitter``. The backend keeps its
    own enum so it has no runtime dependency on ``axon`` —  it only
    consumes the stream.
    """
    SESSION_READY = "session.ready"
    AGENT_MESSAGE_CHUNK = "agent.message_chunk"
    AGENT_THINKING_CHUNK = "agent.thinking_chunk"
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
