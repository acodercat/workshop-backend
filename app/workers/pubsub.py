"""Follow one Redis pub/sub channel for as long as the process lives.

The background tasks that react to something the worker announces — a
terminal turn, on ``settle:notify`` — each follow the channel through
:func:`subscribe_forever` and say only what a message means to them.

Pub/sub is fire-and-forget: a message published while a subscriber is
reconnecting is gone. Every caller is therefore a latency optimization over
a poll that reads the same state, never a correctness path.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis

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


async def subscribe_forever(
    redis: aioredis.Redis,
    channel: str,
    handle: Callable[[str], Awaitable[None]],
) -> None:
    """Call ``handle`` with each payload published on ``channel``, forever.

    Survives a handler that raises (that message is skipped) and reconnects
    after any pub/sub connection drop. The only way to stop is to cancel the
    task — ``asyncio.CancelledError`` propagates so
    :class:`asyncio.TaskGroup` can shut the process down cleanly.
    """
    while True:
        try:
            async with redis.pubsub() as pubsub:
                await pubsub.subscribe(channel)
                logger.info("subscribed on %s", channel)
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
                    # Better to skip one message than tear down the
                    # subscription and trigger a reconnect cycle for it.
                    try:
                        await handle(_text(message["data"]))
                    except Exception:
                        logger.exception("%s: handler failed — message skipped", channel)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "%s: pub/sub error, reconnecting in %.1fs", channel, _RECONNECT_DELAY_SECONDS
            )
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)


def _text(payload: str | bytes) -> str:
    """``decode_responses=True`` on the shared Redis client gives ``str``
    already; ``bytes`` is tolerated should a config change flip that flag."""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload
