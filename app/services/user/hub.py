"""Process-wide in-process fan-out for the per-user notification feed.

One Redis SUBSCRIBE per backend process feeds every local ``/me/events``
SSE connection. The worker PUBLISHes ``{user_id, event}`` to a single
global channel (:data:`~app.services.redis.USER_EVENTS_CHANNEL`); the
hub's reader task routes each event to the in-memory queues of that
user's connections **on this process**.

Redis Pub/Sub broadcasts to every subscriber, so each process receives
every user's events and routes only the ones it has local subscribers
for. That is why connections need no process affinity (no sticky
session): whichever process holds a user's SSE connection also receives
the event and delivers it. Multi-process / multi-node is the native
shape, not a special case.

``session.status_changed`` is **idempotent** state sync (it sets the
current status, not a delta), so Pub/Sub's at-most-once delivery is
acceptable: a dropped event self-heals on the next status change, or
when the client reconnects and re-fetches its session list.
"""

import asyncio
import contextlib
import logging
from collections import defaultdict

import orjson
import redis.asyncio as aioredis

from app.services.redis import USER_EVENTS_CHANNEL

logger = logging.getLogger(__name__)

# Per-connection queue depth. A slow client (backgrounded tab, weak
# network) that can't drain fast enough drops its OLDEST queued event
# rather than stalling the shared reader or its peers — fine for
# idempotent status sync where only the latest state matters.
_QUEUE_MAXSIZE = 16

# How long the reader blocks per ``get_message`` loop. Doubles as the
# pubsub health-check cadence: each loop re-enters ``parse_response``,
# which PINGs a silently-dropped subscription connection (→ surfaces as a
# ConnectionError → resubscribe) instead of blocking forever on a dead
# socket. A plain ``listen()`` would block indefinitely and never
# re-check while idle. Aligned with the pool's 30s health_check_interval.
_READER_TIMEOUT_SECONDS = 30.0


class UserEventHub:
    """Fans Redis-published user events out to in-memory SSE queues.

    One instance per process, owned by the app lifespan. Not thread-safe
    by design: every method runs on the single asyncio event loop.
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._reader_task = asyncio.create_task(self._reader(), name="user-event-hub")

    async def stop(self) -> None:
        if self._reader_task is None:
            return
        self._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader_task

    def subscribe(self, user_id: int) -> asyncio.Queue:
        """Register an SSE connection; returns its private event queue."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers[user_id].add(queue)
        return queue

    def unsubscribe(self, user_id: int, queue: asyncio.Queue) -> None:
        """Deregister a connection on disconnect; drop the user entry when
        its last connection on this process is gone."""
        subscribers = self._subscribers.get(user_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            del self._subscribers[user_id]

    async def _reader(self) -> None:
        """Single SUBSCRIBE for the whole process; route each event to its
        target user's local queues. Resubscribes on a dropped pubsub
        socket — events missed during the reconnect window are tolerated
        (idempotent status sync; see the module docstring).

        Uses ``get_message(timeout=...)`` rather than ``listen()`` so the
        loop periodically re-runs the pubsub health check and a dead
        subscription connection surfaces instead of hanging silently.
        """
        while True:
            pubsub = self._redis.pubsub()
            try:
                await pubsub.subscribe(USER_EVENTS_CHANNEL)
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=_READER_TIMEOUT_SECONDS,
                    )
                    if message is None:
                        continue  # idle tick — also ran the health check
                    try:
                        self._dispatch(message["data"])
                    except Exception:
                        # One malformed payload must not tear down the
                        # reader (which would resubscribe and briefly
                        # stall every connection on this process).
                        logger.warning(
                            "user-event hub: dropping malformed payload",
                            exc_info=True,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "user-event hub reader errored; resubscribing",
                    exc_info=True,
                )
                await asyncio.sleep(1)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()

    def _dispatch(self, raw: str | bytes) -> None:
        payload = orjson.loads(raw)
        queues = self._subscribers.get(payload["user_id"])
        if not queues:
            return  # no local subscribers for this user — another process has them
        event = payload["event"]
        for queue in queues:
            if queue.full():
                queue.get_nowait()  # drop oldest (back-pressure on a slow client)
            queue.put_nowait(event)
