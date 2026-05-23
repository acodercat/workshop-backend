"""Entrypoint for the standalone background-settle process.

Run as:

    uv run python -m app.workers.run

The web server (``app.main:app``) no longer starts background settle
work in its lifespan — separation of concerns. Production deploys
both processes side-by-side (see ``scripts/deploy.sh settle``);
multi-node deploys run this on every host and rely on Postgres
advisory locks (``app.workers.settle_sweeper._LOCK_KEY``) to ensure
exactly one host actually polls per tick.

This process owns **two** concurrent background tasks, both calling
into the same shared settle helpers — push and pull halves of the
same settle responsibility:

- :mod:`app.workers.settle_sweeper` — periodic 60s poll, the
  correctness backstop. Catches missed pub/sub messages, expired
  reservations from dead workers, and reservations whose session was
  deleted.
- :mod:`app.workers.settle_subscriber` — Redis ``settle:notify``
  subscriber, the latency optimization. Worker publishes on terminal
  ``session_turn`` write, subscriber triggers settle within sub-second.

Both are wrapped in :class:`asyncio.TaskGroup` so a fatal exception in
either tears the process down (systemd / scripts/deploy.sh restart
picks it up); the sweeper's per-tick try/except and the subscriber's
outer reconnect loop swallow recoverable errors before they reach
the group.

Graceful shutdown: we deliberately don't install signal handlers.
The advisory lock is auto-released when the process's Postgres
connection closes (on SIGTERM, on OOM, on any abrupt exit), so the
next tick's first survivor picks up the lock without manual cleanup.
"""

import asyncio
import logging

import redis.asyncio as aioredis

from app.logging_config import setup_logging
from app.workers import settle_sweeper, settle_subscriber
from config import settings


async def _run() -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(settle_sweeper.run_forever())
            tg.create_task(settle_subscriber.run_forever(redis))
    finally:
        await redis.aclose()


def main() -> None:
    setup_logging(log_dir="logs", level="INFO")
    logger = logging.getLogger(__name__)
    logger.info("background-settle process starting (sweeper + subscriber)")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("background-settle process exiting (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
