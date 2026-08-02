"""Entrypoint for the standalone background process.

Run as:

    uv run python -m app.workers.run

The web server (``app.main:app``) no longer starts background settle
work in its lifespan — separation of concerns. Production deploys
both processes side-by-side (see ``scripts/deploy.sh background``);
multi-node deploys run this on every host and rely on Postgres
advisory locks (``app.workers.settle_sweeper._LOCK_KEY``) to ensure
exactly one host actually polls per tick.

This process owns up to **three** concurrent background tasks. Two are
the push and pull halves of the same settle responsibility; the third
(flag-gated) enforces mid-turn spend:

- :mod:`app.workers.settle_sweeper` — periodic 60s poll, the
  correctness backstop. Catches missed pub/sub messages, expired
  reservations from dead workers, and reservations whose session was
  deleted.
- :mod:`app.workers.settle_subscriber` — Redis ``settle:notify``
  subscriber, the latency optimization. Worker publishes on terminal
  ``session_turn`` write, subscriber triggers settle within sub-second.
- :mod:`app.workers.budget_enforcer` — only when
  ``budget_enforcer_enabled``: prices each running turn's
  heartbeat-reported interim usage and cancels turns outrunning their
  user's balance. Its own advisory lock as well.

All are wrapped in :class:`asyncio.TaskGroup` so a fatal exception in
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
from app.workers import (
    budget_enforcer,
    settle_sweeper,
    settle_subscriber,
)
from config import settings

logger = logging.getLogger(__name__)


async def _run() -> None:
    # Subscriber connection: a pub/sub ``get_message``/``listen`` is a long
    # blocking read, and ``settle:notify`` is idle most of the time (only
    # published on a terminal turn). A socket read-timeout here would
    # mistake normal idle silence for a failure and churn a
    # reconnect→timeout→reconnect loop, so leave it unset (block forever)
    # and instead rely on a periodic PING (``health_check_interval``) plus
    # TCP keepalive to surface a genuinely dead connection — e.g. one a
    # managed-Redis proxy reaped while idle.
    redis = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=None,
        socket_keepalive=True,
        health_check_interval=30,
    )
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(settle_sweeper.run_forever())
            tg.create_task(settle_subscriber.run_forever(redis))
            # Flag-gated (default off). Log the resolved state either way so a
            # restart makes it unambiguous whether enforcement is live — the
            # enforcer is otherwise silent until it actually stops a turn.
            if settings.budget_enforcer_enabled:
                logger.info(
                    "budget enforcer: ENABLED (interval=%ds buffer=%d credits)",
                    settings.budget_enforcer_interval_seconds,
                    settings.budget_enforcer_buffer_credits,
                )
                tg.create_task(budget_enforcer.run_forever(redis))
            else:
                logger.info("budget enforcer: disabled")
    finally:
        await redis.aclose()


def main() -> None:
    setup_logging(log_dir="logs", level="INFO")
    logger.info(
        "background process starting (settle sweeper + subscriber"
        "%s)",
        " + budget enforcer" if settings.budget_enforcer_enabled else "",
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("background process exiting (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
