"""The per-user notification channel: what the backend publishes on it, and
the SSE generator that tails it.

:func:`publish_session_status` is the backend's half of
``session.status_changed``: the worker announces the terminals it writes
(its ``wire/user_events.py`` is the other half, same payload), and the
backend the transitions it writes itself — ``RUNNING`` at enqueue, and the
``CANCELLED`` / ``FAILED`` of the two recovery paths.

:func:`stream_user_events` is a long-lived async generator that runs
until the HTTP client disconnects: sse-starlette observes the ASGI
``http.disconnect``, cancels the generator's task, and the
``CancelledError`` unwinds through ``await queue.get()`` into the
``finally`` that deregisters the connection from the hub.

Events are fanned out in-process by
:class:`~app.services.user.hub.UserEventHub` (one Redis SUBSCRIBE per
process), not read per-connection from Redis. Carries cross-session
``session.status_changed`` so the sidebar's RUNNING indicator stays in
sync regardless of which page the user is on.
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator

import orjson
import redis.asyncio as aioredis

from app.common.sse import SSEMessage, sse_event
from app.models.session import SessionStatus
from app.services.redis import USER_EVENTS_CHANNEL
from app.services.user.hub import UserEventHub

logger = logging.getLogger(__name__)


async def publish_session_status(
    redis: aioredis.Redis,
    *,
    user_id: int,
    session_id: uuid.UUID,
    status: SessionStatus,
) -> None:
    """Tell every open client of ``user_id`` that a session moved to ``status``.

    Caller invokes **after** the commit. Never raises: the event is
    idempotent state sync, and a client that missed it reconciles on its
    next ``GET /sessions``, so a failed publish must not fail a write that
    already committed.
    """
    payload = {
        "user_id": user_id,
        "event": {
            "type": "session.status_changed",
            "session_id": str(session_id),
            "status": status.value,
            "ts": time.time(),
        },
    }
    try:
        await redis.publish(USER_EVENTS_CHANNEL, orjson.dumps(payload).decode())
    except Exception:
        logger.warning(
            "session %s: status_changed(%s) publish failed",
            session_id,
            status.value,
            exc_info=True,
        )


async def stream_user_events(
    hub: UserEventHub,
    user_id: int,
) -> AsyncIterator[SSEMessage]:
    """Live-tail one user's cross-session notifications via the in-process hub.

    Live-only: emits events that arrive after the connection opens. The
    page's initial sidebar state comes from its ``GET /sessions`` fetch,
    not from this stream — replaying past ``COMPLETE`` / ``FAILED`` lines
    would wrongly flip a still-running session's indicator off. On
    reconnect the client re-fetches that list, so an event missed during
    the gap is reconciled there (``session.status_changed`` is idempotent).
    """
    queue = hub.subscribe(user_id)
    try:
        # Handshake before the first event: lets the client distinguish
        # "connected, idle" from "broken connection", and forces buffering
        # proxies (nginx, CDNs) to flush bytes immediately. Per-connection;
        # never persisted.
        yield sse_event("user.ready", orjson.dumps({"ts": time.time()}).decode())

        while True:
            # Blocks until the hub routes an event here; sse-starlette's
            # own ping keeps the connection alive while idle.
            event = await queue.get()
            fields = {k: v for k, v in event.items() if k != "type"}
            yield sse_event(event["type"], orjson.dumps(fields).decode())
    finally:
        hub.unsubscribe(user_id, queue)
