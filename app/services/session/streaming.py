"""SSE stream generator for live-in-flight sessions.

:func:`stream_events` is a two-phase async generator: replay whatever's
already on the Redis stream, then block-read new entries until the
terminal ``agent.stopped`` event arrives. Fed straight into
:class:`~sse_starlette.sse.EventSourceResponse` by the GET stream
endpoint.

**Pure UI rendering** — this module no longer participates in credit
settle. V2 of the credit subsystem moved the source of truth for
token usage onto ``session_turn.input_tokens`` / ``output_tokens``,
written by the worker in the same fenced transaction that flips
``session_turn.status`` to terminal. Settle is now driven by:

- The lazy sweep on user-facing endpoints (``GET /credits``,
  ``queue_user_message``) — covers the "user comes back" case.
- The background janitor (:mod:`app.workers.credits_janitor`) — covers
  the "user never comes back" case.

The previous SSE eager-settle hook is gone with V2: it was a
compensating mechanism for the Redis-stream race that V2's durable
usage storage eliminates.
"""

import json
import time
from typing import AsyncIterator

import redis.asyncio as aioredis

from app.common.event_types import EventType
from app.common.sse import event_to_sse, sse_event
from app.services.redis import (
    SESSION_STREAM_PREFIX,
    read_stream,
    stream_key,
)


async def stream_events(
    redis: aioredis.Redis,
    session_id: str,
    *,
    last_event_id: str | None = None,
) -> AsyncIterator[dict]:
    """Two-phase SSE generator: replay then follow.

    Phase 1 — replay every Redis stream entry as-is so the client's
    reducer reconstructs the same block structure it would build from a
    live stream. (An older implementation merged ``message_chunk`` events
    into one snapshot blob and silently dropped tool calls, file outputs,
    and status events on cold replay; fixed here by not merging.)

    Phase 2 — block-read new entries as the worker produces them,
    forwarding each until ``agent.stopped`` arrives.

    ``last_event_id`` is the Redis stream entry-id the browser already
    has (sent via the ``Last-Event-ID`` header on EventSource reconnect).
    When set, phase 1 starts strictly *after* that id — Redis 6.2+
    exclusive-range syntax ``(id``. Without this, a reconnect re-emits
    every event since the start, and the frontend reducer is **not**
    idempotent on chunked types: ``message_chunk`` / ``thinking_chunk``
    concatenate text into the trailing markdown block, ``tool_use``
    appends a fresh tool node — replaying duplicates content and tool
    blocks. Resuming from the cursor avoids that.

    If the stream already contains an ``agent.stopped``, only phase 1 runs.
    If the stream doesn't exist yet (worker hasn't started), phase 1 is
    empty and phase 2 blocks until the first entry appears.

    Client disconnect is handled by sse-starlette: it cancels the
    generator task on ``http.disconnect``, and ``CancelledError``
    propagates through the next suspension point.
    """
    # Handshake — see ``SessionReadyEvent`` for the per-connection
    # contract. Fires once per (re)connect, before any Redis replay.
    yield sse_event(
        EventType.SESSION_READY.value,
        data=json.dumps({"ts": str(time.time())}),
    )

    key = stream_key(SESSION_STREAM_PREFIX, session_id)
    range_start = f"({last_event_id}" if last_event_id else "-"
    last_id = last_event_id or "0"

    for entry_id, event in await redis.xrange(key, min=range_start):
        last_id = entry_id
        yield event_to_sse(event, entry_id)
        if event["type"] == EventType.AGENT_STOPPED:
            return

    # Phase 1 drained without a terminal event — we're about to start
    # following live entries. Emit a sentinel so the client knows it
    # can flip from "still replaying history" into normal live mode
    # without relying on a timing heuristic (events at the *end* of a
    # running turn legitimately have fresh ts but are still history).
    yield sse_event(
        EventType.SESSION_LIVE.value,
        data=json.dumps({"ts": str(time.time())}),
    )

    while True:
        entries = await read_stream(redis, key, last_id)
        for _, messages in entries:
            for entry_id, event in messages:
                last_id = entry_id
                yield event_to_sse(event, entry_id)
                if event["type"] == EventType.AGENT_STOPPED:
                    return
        # Empty result → XREAD timed out without new data. Loop again;
        # sse-starlette's own ping (default 15s) handles keepalive.
