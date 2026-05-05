"""Redis service layer — Streams for SSE output, Pub/Sub for worker wake + cancel.

Postgres ``session_turn`` is the queue; this module no longer carries
input-queue concepts. Redis still owns:

- Per-session output stream (``session_stream:{id}``) — worker XADDs
  axon events here, the SSE endpoint forwards to the browser.
- Per-user notification stream (``user_events:{user_id}``) — cross-tab
  status sync.
- Wake pub/sub (``notify:worker``) — backend pings after INSERTing a
  ``session_turn`` so the worker doesn't have to wait for its 5s poll.
  Best-effort: a missed publish is recovered by the poll fallback.
- Cancel pub/sub (``cancel:{session_id}``) — fire-and-forget, reaches
  the worker holding the session (if any).
"""

import uuid

import redis.asyncio as aioredis

# Wire contract — **mirrored** in ``agent-worker/agent_worker/wire/keys.py``
# (``STREAM_PREFIX``, ``USER_EVENTS_PREFIX``, ``NOTIFY_CHANNEL``). Any
# change here must be coordinated there, or the two services silently
# desync.

# Per-session output stream prefix. The worker XADDs events to
# ``session_stream:{session_id}``; this SSE endpoint XRANGE/XREADs from it.
SESSION_STREAM_PREFIX = "session_stream"

# Per-user notification stream prefix. The worker XADDs cross-session
# events (e.g. ``session.status_changed``) to ``user_events:{user_id}``;
# the per-user SSE endpoint forwards them so the sidebar stays in sync
# regardless of which page the user is on.
USER_EVENTS_PREFIX = "user_events"

# Pub/Sub channel the backend publishes to after INSERTing a
# ``session_turn`` row. Wakes idle workers immediately instead of
# making them wait for the 5s poll fallback.
WORKER_NOTIFY_CHANNEL = "notify:worker"

# Pub/Sub channel prefix for per-session cancel signals. Mirrored in
# ``agent-worker/agent_worker/runtime/cancellation.py``; fire-and-forget
# ``PUBLISH cancel:{session_id}`` reaches the worker holding that
# session (if any) and fires :meth:`Agent.cancel` on its loop.
CANCEL_CHANNEL_PREFIX = "cancel"


def stream_key(prefix: str, entity_id: str) -> str:
    return f"{prefix}:{entity_id}"


def user_events_key(user_id: int) -> str:
    """Redis stream key for one user's cross-session notification feed."""
    return stream_key(USER_EVENTS_PREFIX, str(user_id))


def cancel_channel(session_id: str) -> str:
    """Pub/Sub channel for cancelling a specific session."""
    return f"{CANCEL_CHANNEL_PREFIX}:{session_id}"


async def publish_wake(
    redis: aioredis.Redis,
    session_id: uuid.UUID,
) -> None:
    """Notify workers that a new ``session_turn`` row is queued.

    Best-effort fast-path: workers also poll ``claim_next_turn`` every
    few seconds (see ``settings.claim_poll_interval_seconds`` on the
    worker side), so a publish failure or pub/sub disconnect just adds
    one poll cycle of latency — never strands the run.

    Caller invokes **after** the DB transaction that INSERTed the
    ``session_turn`` has committed; otherwise a worker waking on the
    pub/sub could ``claim_next_turn`` before our row is visible and
    re-sleep, defeating the wake's purpose.
    """
    await redis.publish(WORKER_NOTIFY_CHANNEL, str(session_id))


async def read_stream(
    redis: aioredis.Redis,
    key: str,
    cursor: str = "0",
    *,
    count: int = 50,
    block_ms: int = 15_000,
) -> list:
    """Block-read up to ``count`` new entries.

    ``block_ms`` is the **upper bound** on how long XREAD waits before
    returning empty — Redis returns immediately the moment a new entry
    is XADDed, so a long block does not delay live events. The 15s
    default just keeps the calling generator from spinning when the
    stream is idle (10 idle SSE clients × 100ms loop = 100 RPS of
    pointless XREAD traffic).
    """
    return await redis.xread({key: cursor}, count=count, block=block_ms) or []
