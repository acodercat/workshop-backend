"""ASGI entrypoint for the admin console.

Peer of :mod:`app.main` (the public API) and :mod:`app.workers.run` (the
background process) — the third process this backend deploys::

    uv run uvicorn app.admin_main:admin_app --host 127.0.0.1 --port 8006

**Bind to a private interface.** Every route on this app can change money
or account state, and none of it is customer-facing. It is meant to be
reachable from an internal network / VPN only; the public gateway should
have no route to this port. Nothing in the code enforces that — it is a
deployment property, and it is the primary control.

The HTTP stack itself comes from :func:`app.asgi.create_app`, the same
call :mod:`app.main` makes, so the error envelope, rate limiter,
middleware order, request-id correlation, Sentry and ``/metrics`` are
identical across both surfaces by construction rather than by review.
What this module owns is the handful of things that genuinely differ:

- **Its own CORS allowlist and token class**, so neither a
  customer-facing origin nor a customer token can reach an admin route.
- **No Redis client on ``app.state``.** The console streams no SSE and
  enqueues no turns. slowapi still counts in Redis through its own
  ``storage_uri``, so rate limits stay cluster-wide — which is also why
  readiness probes the database only: losing Redis degrades the limiter
  to an in-process fallback rather than failing requests.
- **No startup dependency probe.** ``app.main`` retries the DB
  connection at boot so a Kubernetes pod doesn't crash-loop through a
  few-second dependency race. The console is an internal tool where a
  first request returning 503 is better feedback than a restarting
  process, so readiness is checked on demand instead.
"""

import logging

from app.logging_config import setup_logging

# Before any other app import, so loguru captures module-level loggers
# created during import (same ordering rule as ``app.main``).
setup_logging(log_dir="logs", level="INFO")
logger = logging.getLogger(__name__)

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.admin.router import admin_router
from app.asgi import create_app, probe_database
from app.database import engine
from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown for the console process.

    Nothing to acquire at startup — the DB engine is lazy and there are
    no pools of our own. Shutdown disposes the engine so connections are
    returned rather than left for the server to reap.
    """
    logger.info("Admin console starting up")
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("Admin console shutdown — DB engine disposed")


admin_app = create_app(
    title="MetaSeq Admin API",
    root_path="/admin/api/v1",
    cors_origins=settings.admin_cors_origins,
    lifespan=lifespan,
    readiness_probes={"db": probe_database},
)

admin_app.include_router(admin_router)
