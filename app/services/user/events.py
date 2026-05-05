"""SSE stream generator for the per-user notification channel.

:func:`stream_user_events` is a long-lived async generator that runs
until the HTTP client disconnects: sse-starlette's internal listener
observes the ASGI ``http.disconnect`` and cancels the generator's
task; the ``CancelledError`` propagates through the next suspension
point (``await`` or ``yield``) and unwinds normally.

Carries cross-session events like ``session.status_changed`` so the
sidebar's ``RUNNING`` indicator stays in sync regardless of which page
the user is on.
"""

from typing import AsyncIterator

import redis.asyncio as aioredis

from app.common.sse import event_to_sse
from app.services.redis import read_stream, user_events_key


async def stream_user_events(
    redis: aioredis.Redis,
    user_id: int,
    *,
    last_event_id: str | None = None,
) -> AsyncIterator[dict]:
    """Live-tail the per-user notification stream.

    Fresh connect (``last_event_id`` is ``None``) starts from ``$`` —
    only future entries. Replaying history would overwrite the page's
    initial session-list snapshot with stale ``COMPLETE``/``FAILED``
    lines from past turns, silently flipping the sidebar's ``RUNNING``
    indicator off while a turn is still in flight.

    Reconnect: passing the browser's ``Last-Event-ID`` replays exactly
    the gap the disconnect created.
    """
    key = user_events_key(user_id)
    cursor = last_event_id or "$"

    while True:
        entries = await read_stream(redis, key, cursor)
        for _, messages in entries:
            for entry_id, event in messages:
                cursor = entry_id
                yield event_to_sse(event, entry_id)
        # Empty result → XREAD timed out without new data. Loop again;
        # sse-starlette's own ping (default 15s) handles keepalive.
