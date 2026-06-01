import asyncio
import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import BlockingConnectionPool, ConnectionPool
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_config import setup_logging

setup_logging(log_dir="logs", level="INFO")
logger = logging.getLogger(__name__)

from app.api.endpoints import auth, credits, feedback, session, share, skill, user
from app.database import engine
from app.errors import AppError
from app.exception_handlers import (
    app_error_handler,
    general_exception_handler,
    http_exception_handler,
    integrity_error_handler,
    sqlalchemy_exception_handler,
    validation_exception_handler,
)
from app.observability import install_observability
from app.rate_limit import limiter, rate_limit_exceeded_handler
from config import settings
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

ROUTERS = [
    auth.router,
    credits.router,
    feedback.router,
    session.router,
    share.router,
    skill.router,
    user.router,
]


# Sentinel: distinguishes "caller did not specify socket_timeout (inherit
# whatever redis_url carries)" from "caller explicitly set it (incl. None)".
_INHERIT_SOCKET_TIMEOUT = object()


def _make_redis_client(
    url: str,
    *,
    max_connections: int,
    blocking: bool,
    pool_timeout: float | None = None,
    socket_timeout: object = _INHERIT_SOCKET_TIMEOUT,
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
    """
    pool_cls = BlockingConnectionPool if blocking else ConnectionPool
    pool_kwargs: dict = {
        "max_connections": max_connections,
        "decode_responses": True,
        # Probe pooled connections on next-use so a silently-dropped
        # TCP socket (Redis OOM, kernel half-open, middleware drop
        # without RST) surfaces as a prompt ``ConnectionError`` instead
        # of an indefinite ``recv``. The 30s cadence is no-op on
        # healthy connections; SSE long-poll commands are unaffected
        # because the check fires only at connection checkout.
        "health_check_interval": 30,
    }
    if blocking and pool_timeout is not None:
        pool_kwargs["timeout"] = pool_timeout
    pool = pool_cls.from_url(url, **pool_kwargs)
    if socket_timeout is not _INHERIT_SOCKET_TIMEOUT:
        # ``from_url`` does ``kwargs.update(parse_url(url))`` — a
        # ``socket_timeout`` baked into redis_url overrides one passed as a
        # kwarg. So force ours onto the pool's per-connection kwargs AFTER
        # construction; otherwise the streaming pool's long read timeout
        # would lose to a short socket_timeout in the URL and blocking
        # XREADs would die mid-poll.
        pool.connection_kwargs["socket_timeout"] = socket_timeout
    return aioredis.Redis(connection_pool=pool)


async def _ping_redis(
    url: str,
    *,
    max_connections: int,
    blocking: bool = False,
    pool_timeout: float | None = None,
    socket_timeout: object = _INHERIT_SOCKET_TIMEOUT,
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
                attempt, settings.startup_probe_attempts, exc, delay,
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
                attempt, settings.startup_probe_attempts, exc, delay,
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
    )
    # Streaming pool: SSE long-poll XREADs hold connections for ``block_ms``
    # seconds each. Default ``ConnectionPool`` semantics — once full the
    # next connect attempt fails fast, which is the right back-pressure
    # signal (the box is already at its SSE capacity).
    app.state.redis_streaming = await _ping_redis(
        settings.redis_url,
        max_connections=settings.redis_streaming_pool_size,
        blocking=False,
        # Override any short ``socket_timeout`` in redis_url: a blocking
        # XREAD holds the socket for up to block_ms (15s), so the read
        # timeout must exceed that or the SSE poll dies mid-block.
        socket_timeout=settings.redis_streaming_socket_timeout_seconds,
    )
    logger.info(
        "Redis connected — short-op pool=%s (blocking, timeout=%ss), "
        "streaming pool=%s (fail-fast)",
        settings.redis_pool_size,
        settings.redis_pool_timeout_seconds,
        settings.redis_streaming_pool_size,
    )

    # Background-settle process (sweeper + subscriber) runs as a
    # **sibling**, not inside this event loop — see ``app/workers/run.py``
    # and ``scripts/deploy.sh settle``. Web's lifespan is HTTP-only
    # again: separating background settle work from request handling
    # lets each scale, restart, and fail independently.

    logger.info("API started")
    try:
        yield
    finally:
        await app.state.redis.close()
        await app.state.redis_streaming.close()
        await engine.dispose()
        logger.info("API shutdown — Redis pools closed, DB engine disposed")


app = FastAPI(
    title="MetaSeq Agent API",
    version="1.0",
    root_path="/api/v1",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(IntegrityError, integrity_error_handler)
app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# SlowAPIMiddleware makes ``limiter.default_limits`` an actual global
# floor. Without it slowapi only enforces limits on endpoints carrying
# an explicit ``@limiter.limit`` decorator — every other route is
# completely unthrottled (the default 200/min is inert). Added BEFORE
# CORS so CORS wraps it: a 429 response still gets its
# ``Access-Control-Allow-Origin`` headers, otherwise the browser would
# surface an opaque network error instead of the rate-limit JSON. (The
# request-id middleware, added in ``install_observability`` below, then
# wraps CORS as the outermost layer — it only tags the request, so the
# CORS-wraps-SlowAPI guarantee is unaffected.)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # So the browser can read the correlation id off a response and put
    # it in a bug report / error toast.
    expose_headers=["X-Request-ID"],
)

# Sentry + Prometheus /metrics + the request-id middleware. Installed
# after CORS so the request-id layer is outermost (correlation id bound
# before anything inner logs); CORS stays inside it, still wrapping the
# routes so error responses keep their CORS headers.
install_observability(app)

for router in ROUTERS:
    app.include_router(router)


@app.get("/")
@limiter.exempt
async def root():
    return {"message": "Welcome to MetaSeq Agent API"}


# Health probes are exempt: K8s liveness/readiness poll these every
# few seconds per node — a per-IP 200/min cap would 429 the probe,
# K8s would mark the pod unhealthy and kill it.
@app.get("/health/live")
@limiter.exempt
async def health_live():
    """Liveness probe — process is up. Always 200 unless the loop is dead."""
    return {"status": "ok"}


@app.get("/health/ready")
@limiter.exempt
async def health_ready():
    """Readiness probe — DB and Redis both reachable.

    Returns 503 when either dependency is down so K8s stops routing
    traffic; the pod stays running (liveness still 200) so it can
    recover when the dependency comes back without a restart cycle.
    """
    errors: list[str] = []
    try:
        await app.state.redis.ping()
    except (RedisError, ConnectionError, OSError) as exc:
        errors.append(f"redis: {exc}")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        errors.append(f"db: {exc}")
    if errors:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not ready", "errors": errors},
        )
    return {"status": "ok"}
