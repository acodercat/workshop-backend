"""SSE stream generator for the per-user notification channel.

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

import time
from typing import AsyncIterator

import orjson

from app.common.sse import SSEMessage, sse_event
from app.services.user.hub import UserEventHub


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
