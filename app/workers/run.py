"""Entrypoint for the standalone credits-janitor process.

Run as:

    uv run python -m app.workers.run

The web server (``app.main:app``) no longer starts the janitor in its
lifespan — separation of concerns. Production deploys both processes
side-by-side (see ``scripts/deploy.sh janitor``); multi-node deploys
run a janitor on every host and rely on Postgres advisory locks
(``app.workers.credits_janitor._LOCK_KEY``) to make sure exactly one
host actually sweeps per tick.

Graceful shutdown: we deliberately don't install signal handlers.
The advisory lock is auto-released when the process's Postgres
connection closes (on SIGTERM, on OOM, on any abrupt exit), so the
next tick's first survivor picks up the lock without manual cleanup.
Cancellation semantics inside ``run_forever`` cover the asyncio side.
"""

import asyncio
import logging

from app.logging_config import setup_logging
from app.workers import credits_janitor


def main() -> None:
    setup_logging(log_dir="logs", level="INFO")
    logger = logging.getLogger(__name__)
    logger.info("credits-janitor process starting (interval=%ss)", 60)
    try:
        asyncio.run(credits_janitor.run_forever())
    except KeyboardInterrupt:
        logger.info("credits-janitor process exiting (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
