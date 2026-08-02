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
import re
import time
from enum import Enum
from typing import AsyncIterator

import redis.asyncio as aioredis
from sqlalchemy import func, select

from app.common.event_types import EventType
from app.common.sse import event_to_sse, sse_event
from app.database import AsyncSessionLocal
from app.models.session import Session, SessionStatus
from app.models.session_turn import SessionTurn
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

# A Redis stream entry id is ``<ms>-<seq>`` (both integers). The browser
# echoes back the ``id:`` we stamp on each SSE frame, so a well-behaved
# ``Last-Event-ID`` always matches this — but the header is client-controlled.
_STREAM_ID_RE = re.compile(r"\A\d+-\d+\Z")

# Entries pulled per ``xrange`` round-trip during phase-1 replay. Bounds the
# replay's peak memory to one page (vs materializing the whole stream, up to
# the worker's ~100k MAXLEN, in a single reply) at the cost of ceil(N/page)
# round-trips against a local Redis.
_REPLAY_PAGE_SIZE = 500

# Wall-clock ceiling on how long the follow loop stays open while it can
# *neither* follow Redis (idle / empty blocks) *nor* reach Postgres to confirm
# the session is still running. Past this, we stop holding a blind generator
# open and let the client's EventSource reconnect and re-evaluate, rather than
# spinning for the full stream TTL under a dual Redis+DB outage.
_BLIND_FUSE_SECONDS = 120.0


class _Liveness(Enum):
    """Tri-state result of the DB liveness check used by the SSE fallback."""

    TERMINAL = "terminal"  # reached a terminal status (or the row is gone)
    RUNNING = "running"  # definitively still running
    UNKNOWN = "unknown"  # DB unreachable — couldn't determine; arms the fuse


def _safe_cursor(last_event_id: str | None) -> str | None:
    """Validate a client-supplied ``Last-Event-ID`` as a Redis entry id.

    The value is client-controlled, so a malformed one (truncated, injected
    range syntax like ``(`` or ``+``, non-numeric) must not reach
    ``xrange(min=...)``, where it would raise and tear the stream down before
    the first real event is yielded. Anything that isn't a well-formed entry
    id is treated as "no cursor" → full replay from ``-``: safe (the reducer
    rebuilds block state from scratch), only slightly more redundant.
    """
    if not last_event_id:
        return None
    if _STREAM_ID_RE.match(last_event_id):
        return last_event_id
    logger.warning(
        "stream_events: ignoring malformed Last-Event-ID %r; replaying "
        "from stream start", last_event_id,
    )
    return None


async def _session_liveness(session_id: str) -> _Liveness:
    """Classify the session's liveness from its Postgres ``status``.

    Opens its own short-lived session — the SSE generator deliberately
    holds no DB connection across its lifetime (it reads Redis only; see
    the stream endpoint), so this must not pin one either. Consulted only
    after an idle XREAD block, never on the hot path.

    A missing row (session deleted mid-stream) counts as ``TERMINAL`` so the
    stream closes rather than blocking forever. A DB error returns ``UNKNOWN``
    — the caller re-blocks and the live Redis terminal (or a later check once
    the DB recovers) still resolves the stream; a transient blip must not tear
    down an otherwise-healthy SSE. *Persistent* ``UNKNOWN`` on an idle stream
    is what arms the caller's blind-fuse.
    """
    try:
        async with AsyncSessionLocal() as db:
            status = await db.scalar(
                select(Session.status).where(Session.id == session_id)
            )
    except Exception:
        logger.warning(
            "stream_events: liveness check failed for session %s; "
            "will retry next idle block", session_id, exc_info=True,
        )
        return _Liveness.UNKNOWN
    if status is None or status in _TERMINAL_STATUSES:
        return _Liveness.TERMINAL
    return _Liveness.RUNNING


async def _current_turn_id(session_id: str) -> str | None:
    """The session's newest turn id — the one this SSE connection serves.

    Returned as a **string** to compare directly against the Redis-stored
    ``turn_id`` stamp. Resolved once at generator start via a short-lived
    session (same posture as :func:`_session_liveness`); the current turn
    is fixed for one connection (a new turn flips ``status`` → the client
    reconnects and this re-resolves). ``None`` on a DB error or a session
    with no turns yet → filtering is skipped (fail-open: never drop a real
    event because we couldn't resolve the turn).
    """
    try:
        async with AsyncSessionLocal() as db:
            turn_id = await db.scalar(
                select(func.max(SessionTurn.id)).where(
                    SessionTurn.session_id == session_id
                )
            )
    except Exception:
        logger.warning(
            "stream_events: current-turn lookup failed for session %s; "
            "turn-id filtering disabled this connection", session_id,
            exc_info=True,
        )
        return None
    return str(turn_id) if turn_id is not None else None


def _is_stale_entry(event: dict, current_turn_id: str | None) -> bool:
    """True when a stream entry belongs to a turn other than the current one.

    A worker that lost its lease mid-turn keeps XADDing for up to a
    heartbeat interval; after enqueue DELETEs and the next turn recreates
    the same stream key, those stale entries interleave into the next
    turn's view. Each entry carries the ``turn_id`` it was produced under,
    so a mismatch against the session's current turn is dropped. Entries
    with no ``turn_id`` (pre-rollout workers, or when the current turn
    couldn't be resolved) are never treated as stale.
    """
    if current_turn_id is None:
        return False
    entry_turn_id = event.get("turn_id")
    return entry_turn_id is not None and entry_turn_id != current_turn_id


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
    forwarding each until ``agent.stopped`` arrives. Two fallbacks cover a
    lost terminal: if an idle block finds the session already terminal in
    Postgres, a synthetic ``agent.stopped`` closes the stream; if Redis and
    Postgres are *both* unreachable past ``_BLIND_FUSE_SECONDS``, the stream
    closes so the client reconnects rather than following a dead stream.

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
    cursor = _safe_cursor(last_event_id)
    range_start = f"({cursor}" if cursor else "-"
    last_id = cursor or "0"

    # The turn this connection serves. Entries stamped with a different
    # turn_id (a zombie worker leaking its dead turn's events into the
    # stream key the current turn recreated) are dropped below. Resolved
    # once — the current turn is fixed for a connection's lifetime.
    current_turn_id = await _current_turn_id(session_id)

    # Phase 1 — replay the backlog one page at a time. Each page advances
    # ``range_start`` to an exclusive cursor past the last entry seen (Redis
    # 6.2+ ``(id``), so pages don't overlap or skip. Whatever the worker
    # appends mid-replay is caught by the follow loop below, which resumes
    # from ``last_id`` — the phase boundary is intentionally fuzzy.
    while True:
        batch = await redis.xrange(key, min=range_start, count=_REPLAY_PAGE_SIZE)
        if not batch:
            break
        for entry_id, event in batch:
            last_id = entry_id
            if _is_stale_entry(event, current_turn_id):
                continue  # cursor still advances; a stale terminal can't close us
            yield event_to_sse(event, entry_id)
            if event["type"] == EventType.AGENT_STOPPED:
                return
        if len(batch) < _REPLAY_PAGE_SIZE:
            break  # short page — the backlog is drained; skip an empty xrange
        range_start = f"({last_id}"

    # Phase 1 drained without a terminal event — we're about to start
    # following live entries. Emit a sentinel so the client knows it
    # can flip from "still replaying history" into normal live mode
    # without relying on a timing heuristic (events at the *end* of a
    # running turn legitimately have fresh ts but are still history).
    yield sse_event(
        EventType.SESSION_LIVE.value,
        data=json.dumps({"ts": str(time.time())}),
    )

    # Monotonic reference for when the current fully-blind streak began (Redis
    # idle *and* DB unreachable), or None when not blind. Drives the fuse below.
    blind_since: float | None = None
    while True:
        entries = await read_stream(redis, key, last_id)
        for _, messages in entries:
            for entry_id, event in messages:
                last_id = entry_id
                if _is_stale_entry(event, current_turn_id):
                    continue  # drop a dead turn's leaked events
                yield event_to_sse(event, entry_id)
                if event["type"] == EventType.AGENT_STOPPED:
                    return
        if entries:
            # Got live data this cycle — keep following. Only consult the
            # DB fallback after a genuinely idle block. Redis is healthy,
            # so reset the blind streak.
            blind_since = None
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
        # A still-``RUNNING`` session never false-closes here, so a
        # legitimately slow turn (long tool call, cold-start sandbox) is safe.
        liveness = await _session_liveness(session_id)
        if liveness is _Liveness.TERMINAL:
            yield sse_event(
                EventType.AGENT_STOPPED.value,
                data=json.dumps({"content": ""}),
            )
            return
        if liveness is _Liveness.RUNNING:
            # Still running and the DB is reachable — loop again;
            # sse-starlette's own ping (default 15s) handles keepalive.
            blind_since = None
            continue
        # UNKNOWN: the stream is idle *and* Postgres is unreachable, so we
        # can neither follow nor verify. Under a transient blip we just
        # re-block and recover on the next cycle — but a *sustained* dual
        # outage would otherwise spin this generator blind for the full
        # stream TTL. Arm a wall-clock fuse: once we've been fully blind
        # past the window, close the SSE so the client's EventSource
        # reconnects and re-evaluates (resuming from ``Last-Event-ID``)
        # rather than us holding a zombie generator open.
        now = time.monotonic()
        if blind_since is None:
            blind_since = now
        elif now - blind_since >= _BLIND_FUSE_SECONDS:
            logger.error(
                "stream_events: session %s blind for %.0fs (Redis idle + DB "
                "unreachable); closing SSE so the client reconnects",
                session_id, now - blind_since,
            )
            return
