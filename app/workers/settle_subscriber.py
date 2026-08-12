"""Event-driven settle: subscribe to ``settle:notify`` pub/sub.

Closes the latency gap left by the polling sweeper. The worker
publishes a session_id on this channel right after committing a
terminal ``session_turn`` write; we receive it and call
``credits.settle_open_for_session`` immediately, taking the typical
settle delay from "next user request / 60s sweeper tick" down to
sub-second.

Pub/sub is fire-and-forget. A message lost to a disconnect, a slow
restart, or a network blip is **expected** and not an error case —
:mod:`app.workers.settle_sweeper` will pick that session up on its
next 60s sweep. This subscriber is a latency optimization, not a
correctness path.

Co-located with the sweeper in the same sibling process (see
``app/workers/run.py``): both are "background settle work" and share
a Redis client.
"""

import asyncio
import logging
from uuid import UUID

import redis.asyncio as aioredis

from app.database import AsyncSessionLocal
from app.services import credits
from app.services.redis import SETTLE_NOTIFY_CHANNEL

logger = logging.getLogger(__name__)

# Sleep before resubscribing after a pub/sub connection drop. Small
# enough that real outages recover quickly; large enough not to spin
# on a dead Redis. Mirrors the worker's CancelListener reconnect cadence.
_RECONNECT_DELAY_SECONDS = 1.0

# How long ``get_message`` waits for a message before returning ``None``.
# A returned ``None`` is a normal idle tick (the channel is quiet), not an
# error — the loop just waits again. Also bounds how often the connection's
# health-check PING gets a chance to run between reads.
_POLL_TIMEOUT_SECONDS = 30.0


async def run_forever(redis: aioredis.Redis) -> None:
    """Subscribe to ``settle:notify`` forever, settling on each message.

    Survives any per-message failure (bad payload, transient DB error)
    and reconnects after any pub/sub connection drop. The only way to
    stop the subscriber is to cancel the task — ``asyncio.CancelledError``
    propagates so :class:`asyncio.TaskGroup` can shut the process down
    cleanly.
    """
    while True:
        try:
            async with redis.pubsub() as pubsub:
                await pubsub.subscribe(SETTLE_NOTIFY_CHANNEL)
                logger.info("settle subscriber subscribed on %s", SETTLE_NOTIFY_CHANNEL)
                # Poll with an explicit timeout instead of ``listen()``: on an
                # idle channel ``get_message`` returns ``None`` when the window
                # elapses (normal idle, NOT an error), where a blocking
                # ``listen()`` read raises ``TimeoutError`` if the client has a
                # socket read-timeout. Looping also lets the connection's
                # health-check PING fire between reads to detect a dead socket.
                # ``ignore_subscribe_messages`` drops the subscribe confirmation
                # so only real ``message`` payloads reach the handler.
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=_POLL_TIMEOUT_SECONDS,
                    )
                    if message is None:
                        continue  # idle window elapsed — keep waiting
                    # ``_handle_message`` already swallows expected
                    # errors (bad payload, DB blip). The outer guard
                    # here is defense against a programming bug in
                    # the handler — better to skip one message than
                    # tear down the subscription and trigger a
                    # reconnect cycle for it.
                    try:
                        await _handle_message(message["data"])
                    except Exception:
                        logger.exception(
                            "settle subscriber: unexpected handler error — skipping",
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "settle subscriber: pub/sub error, reconnecting in %.1fs",
                _RECONNECT_DELAY_SECONDS,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)


async def _handle_message(payload: str | bytes) -> None:
    """Parse the session_id payload and trigger settle.

    Any exception is logged and swallowed — the sweeper backstops a
    settle that fails here, and we must not kill the subscriber loop
    on a single bad row.
    """
    # ``decode_responses=True`` on the shared Redis client gives us
    # ``str`` already; tolerate ``bytes`` for safety if a future config
    # change flips that flag.
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    try:
        session_id = UUID(payload)
    except (ValueError, TypeError):
        logger.warning("settle subscriber: invalid payload %r — skipping", payload)
        return
    try:
        async with AsyncSessionLocal() as db:
            await credits.settle_open_for_session(db, session_id)
            await db.commit()
    except Exception:
        logger.exception(
            "settle subscriber: settle_open_for_session failed for %s — sweeper will retry",
            session_id,
        )
