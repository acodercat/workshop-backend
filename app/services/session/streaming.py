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
- The background sweeper (:mod:`app.workers.settle_sweeper`) and
  pub/sub subscriber (:mod:`app.workers.settle_subscriber`) — cover
  the "user never comes back" case.

The previous SSE eager-settle hook is gone with V2: it was a
compensating mechanism for the Redis-stream race that V2's durable
usage storage eliminates.
"""

import json
import logging
import time
from typing import AsyncIterator

import redis.asyncio as aioredis
from sqlalchemy import select

from app.common.event_types import EventType
from app.common.sse import event_to_sse, sse_event
from app.database import AsyncSessionLocal
from app.models.session import Session, SessionStatus
from app.services.redis import (
    SESSION_STREAM_PREFIX,
    read_stream,
    stream_key,
)

logger = logging.getLogger(__name__)

# session.status values that mean the turn is over. The worker writes one
# of these in the same fenced transaction that *precedes* the Redis
# ``agent.stopped`` emit, so a terminal status with no terminal stream
# event means the event was lost (Redis blip swallowed the XADD, or the
# worker died between the DB commit and the emit).
#
# Defined here rather than on ``SessionStatus``: this is service-layer
# liveness logic, and the enum ships no terminal-set helper (the rest of
# the codebase checks ``== RUNNING`` / ``!= RUNNING`` at its gates, never
# a terminal set), so there's nothing to reuse and nothing else to keep
# in sync.
_TERMINAL_STATUSES = frozenset(
    {SessionStatus.COMPLETE, SessionStatus.CANCELLED, SessionStatus.FAILED}
)


async def _session_is_terminal(session_id: str) -> bool:
    """Whether the session has reached a terminal ``status`` in Postgres.

    Opens its own short-lived session — the SSE generator deliberately
    holds no DB connection across its lifetime (it reads Redis only; see
    the stream endpoint), so this must not pin one either. Consulted only
    after an idle XREAD block, never on the hot path.

    A missing row (session deleted mid-stream) counts as terminal so the
    stream closes rather than blocking forever. Any DB error is swallowed
    as "not terminal" — the caller simply re-blocks and the live Redis
    terminal (or the next idle check) still resolves the stream; a transient
    DB blip must not tear down an otherwise-healthy SSE connection.
    """
    try:
        async with AsyncSessionLocal() as db:
            status = await db.scalar(
                select(Session.status).where(Session.id == session_id)
            )
    except Exception:
        logger.warning(
            "stream_events: terminal-status check failed for session %s; "
            "will retry next idle block", session_id, exc_info=True,
        )
        return False
    return status is None or status in _TERMINAL_STATUSES


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
        if entries:
            # Got live data this cycle — keep following. Only consult the
            # DB fallback after a genuinely idle block.
            continue
        # Empty block. The terminal ``agent.stopped`` reaches us only via
        # Redis, but a blip can swallow that XADD (the worker's stream
        # writes are best-effort — Postgres is the durable truth) and a
        # crash can drop it between the DB commit and the emit. Either way
        # this loop would otherwise block forever, hanging the SSE client
        # and every later replay within the stream TTL. Consult the source
        # of truth: if the session is already terminal, the event is not
        # coming — synthesize ``agent.stopped`` (empty content, matching
        # the worker's ``emit_terminal_done`` / recovery-cron shape) so the
        # client closes cleanly and re-fetches the consistent DB state.
        # A still-``RUNNING`` session returns False here, so a legitimately
        # slow turn (long tool call, cold-start sandbox) never false-closes.
        if await _session_is_terminal(session_id):
            yield sse_event(
                EventType.AGENT_STOPPED.value,
                data=json.dumps({"content": ""}),
            )
            return
        # Still running — loop again; sse-starlette's own ping (default
        # 15s) handles keepalive.
