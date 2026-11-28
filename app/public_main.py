"""ASGI entrypoint for the public (customer) API.

Peer of :mod:`app.admin_main` (the operations console) and
:mod:`app.workers.run` (the background process) — the internet-facing
one of the three processes this backend deploys::

    uv run uvicorn app.public_main:app --host 0.0.0.0 --port 8005

The HTTP stack comes from :func:`app.asgi.create_app`, shared with the
console; what this module owns is the customer surface's genuine
differences — the two Redis pools with their startup probes, the
per-user event hub, and the public CORS allowlist. Routes come from
:mod:`app.api.public.router`.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI
from redis.asyncio import BlockingConnectionPool, ConnectionPool
from redis.asyncio.connection import parse_url
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    RedisError,
    TimeoutError as RedisTimeoutError,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.logging_config import setup_logging

setup_logging(log_dir="logs", level="INFO")
logger = logging.getLogger(__name__)

from app.api.public.router import public_router
from app.asgi import create_app, probe_database
from app.database import engine
from app.rate_limit import limiter
from app.services.user import UserEventHub
from config import settings


def _make_redis_client(
    url: str,
    *,
    max_connections: int,
    blocking: bool,
    pool_timeout: float | None = None,
    socket_timeout: float | None = None,
) -> aioredis.Redis:
    """Build a Redis client backed by the requested pool class.

    ``blocking=True`` uses :class:`BlockingConnectionPool`: when the pool
    is full, callers wait up to ``pool_timeout`` seconds for a free
    connection before raising. Right for short ops where queueing
    briefly is preferable to a 5xx during a traffic burst.

    ``blocking=False`` uses the default :class:`ConnectionPool`, which
    raises immediately when full. Right for SSE long-polls — once the
    cap is hit, it means the box is already serving its target number
    of streams; rejecting the next connection cleanly beats letting it
    pile up.

    ``socket_timeout`` is a per-pool **behaviour** knob (the short-op pool
    wants a short fast-fail; the streaming pool must outlast a blocking
    XREAD), so it is owned by config and passed in explicitly — never read
    from the URL query string, where a single value can't serve both pools
    and would be ambiguous. ``parse_url`` extracts only the connection
    target (host/port/db/auth/ssl); our kwargs layer on top, so the
    explicit ``socket_timeout`` always wins regardless of the URL.
    """
    pool_cls = BlockingConnectionPool if blocking else ConnectionPool
    # ``parse_url`` answers a ``ConnectKwargs`` TypedDict, which names only the
    # URL-derived keys; the pool options layered on below are not in it.
    conn_kwargs: dict[str, Any] = dict(parse_url(url))
    conn_kwargs.update(
        max_connections=max_connections,
        decode_responses=True,
        socket_timeout=socket_timeout,
        # Dead-connection handling — three cooperating layers so a
        # connection the Redis server reaped for idleness never surfaces
        # as a failed request:
        #   * health_check_interval: on checkout, PING first when the
        #     connection has been idle this long, to prove it's alive.
        #   * socket_timeout (above): bounds that PING — and every command
        #     — so a half-open socket fails in seconds, not after the
        #     kernel's minutes-long TCP retransmits (errno 110).
        #   * retry / retry_on_error: on that timeout, drop the dead
        #     connection and rerun the command on a fresh one.
        health_check_interval=30,
        retry=Retry(ExponentialBackoff(cap=0.5, base=0.05), retries=2),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )
    if blocking and pool_timeout is not None:
        conn_kwargs["timeout"] = pool_timeout
    return aioredis.Redis(connection_pool=pool_cls(**conn_kwargs))


async def _ping_redis(
    url: str,
    *,
    max_connections: int,
    blocking: bool = False,
    pool_timeout: float | None = None,
    socket_timeout: float | None = None,
) -> aioredis.Redis:
    """Connect to Redis with bounded exponential backoff and a sized pool.

    Container orchestrators (k8s, compose) routinely start dependencies
    in parallel; failing fast on the first ``ping()`` would force the
    pod into CrashLoopBackOff for what is usually a few-second startup
    race. Retries are capped so a *real* outage still surfaces.
    """
    delay = settings.startup_probe_base_delay_seconds
    last_error: Exception | None = None
    for attempt in range(1, settings.startup_probe_attempts + 1):
        client = _make_redis_client(
            url,
            max_connections=max_connections,
            blocking=blocking,
            pool_timeout=pool_timeout,
            socket_timeout=socket_timeout,
        )
        try:
            await client.ping()
            return client
        except (RedisError, ConnectionError, OSError) as exc:
            last_error = exc
            await client.close()
            if attempt == settings.startup_probe_attempts:
                break
            logger.warning(
                "Redis ping failed (attempt %s/%s): %s — retrying in %.1fs",
                attempt,
                settings.startup_probe_attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, settings.startup_probe_max_delay_seconds)
    raise RuntimeError(f"Redis not reachable after retries: {last_error!r}")


async def _ping_db(db_engine: AsyncEngine) -> None:
    """Verify the DB is reachable with the same retry policy as Redis."""
    delay = settings.startup_probe_base_delay_seconds
    last_error: Exception | None = None
    for attempt in range(1, settings.startup_probe_attempts + 1):
        try:
            async with db_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except (SQLAlchemyError, OSError) as exc:
            last_error = exc
            if attempt == settings.startup_probe_attempts:
                break
            logger.warning(
                "DB ping failed (attempt %s/%s): %s — retrying in %.1fs",
                attempt,
                settings.startup_probe_attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, settings.startup_probe_max_delay_seconds)
    raise RuntimeError(f"Database not reachable after retries: {last_error!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _ping_db(engine)
    logger.info("DB connected")

    # Short-op pool: queue under burst (BlockingConnectionPool) so a
    # spike of enqueue/publish/cancel doesn't surface as 5xx. ``timeout``
    # bounds the queue wait — beyond that we give up and let the caller
    # see the error.
    app.state.redis = await _ping_redis(
        settings.redis_url,
        max_connections=settings.redis_pool_size,
        blocking=True,
        pool_timeout=settings.redis_pool_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )
    # Streaming pool: SSE long-poll XREADs hold connections for ``block_ms``
    # seconds each. Default ``ConnectionPool`` semantics — once full the
    # next connect attempt fails fast, which is the right back-pressure
    # signal (the box is already at its SSE capacity).
    app.state.redis_streaming = await _ping_redis(
        settings.redis_url,
        max_connections=settings.redis_streaming_pool_size,
        blocking=False,
        # A blocking XREAD holds the socket for up to block_ms (15s), so the
        # streaming pool needs a read timeout longer than that, where the
        # short-op pool's is short. See redis_streaming_socket_timeout_seconds.
        socket_timeout=settings.redis_streaming_socket_timeout_seconds,
    )
    logger.info(
        "Redis connected — short-op pool=%s (blocking, timeout=%ss), streaming pool=%s (fail-fast)",
        settings.redis_pool_size,
        settings.redis_pool_timeout_seconds,
        settings.redis_streaming_pool_size,
    )

    # In-process fan-out for /me/events: one Redis SUBSCRIBE per process
    # feeds every local SSE connection (see app/services/user/hub.py).
    app.state.user_event_hub = UserEventHub(app.state.redis)
    await app.state.user_event_hub.start()

    # Background process (settle sweeper + subscriber, schedule and message
    # queue dispatchers, budget enforcer) runs as a **sibling**, not inside this event loop — see
    # ``app/workers/run.py`` and ``scripts/deploy.sh background``. Web's
    # lifespan is HTTP-only again: separating background work from request
    # handling lets each scale, restart, and fail independently.

    logger.info("API started")
    try:
        yield
    finally:
        await app.state.user_event_hub.stop()
        await app.state.redis.aclose()
        await app.state.redis_streaming.aclose()
        await engine.dispose()
        logger.info("API shutdown — Redis pools closed, DB engine disposed")


async def _probe_redis(app: FastAPI) -> str | None:
    """Readiness probe for the short-op Redis pool.

    Only this app has Redis on ``app.state``, so the probe lives here
    rather than in :mod:`app.asgi` — the console has no pool to check.
    """
    try:
        await app.state.redis.ping()
    except (RedisError, ConnectionError, OSError) as exc:
        return str(exc)
    return None


app = create_app(
    title="HKGAI Agent API",
    root_path="/api/v1",
    cors_origins=settings.cors_origins,
    lifespan=lifespan,
    readiness_probes={"redis": _probe_redis, "db": probe_database},
)

app.include_router(public_router)


@app.get("/")
@limiter.exempt
async def root():
    return {"message": "Welcome to HKGAI Agent API"}
