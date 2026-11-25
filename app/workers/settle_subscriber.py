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

import logging
from uuid import UUID

import redis.asyncio as aioredis

from app.database import AsyncSessionLocal
from app.services import credits
from app.services.redis import SETTLE_NOTIFY_CHANNEL
from app.workers.pubsub import subscribe_forever

logger = logging.getLogger(__name__)


async def run_forever(redis: aioredis.Redis) -> None:
    """Subscribe to ``settle:notify`` forever, settling on each message."""
    await subscribe_forever(redis, SETTLE_NOTIFY_CHANNEL, _handle_message)


async def _handle_message(payload: str) -> None:
    """Parse the session_id payload and trigger settle.

    Any exception is logged and swallowed — the sweeper backstops a
    settle that fails here.
    """
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
