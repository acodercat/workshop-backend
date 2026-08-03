"""Shared construction for every HTTP surface this backend serves.

There are two ASGI apps — the customer API (:mod:`app.main`) and the
admin console (:mod:`app.admin_main`) — and they must agree on everything
that is not a *deliberate* difference: the error envelope, the rate
limiter, the middleware **order**, request-id correlation, Sentry,
``/metrics``, and the shape of the health probes.

Keeping that agreement by copy-paste fails the moment there are two
copies. :func:`create_app` owns it instead, so a change to the stack
reaches both surfaces or neither, and the per-app differences are exactly
the arguments it takes — nothing else varies:

=====================  =========================  ===============================
                       ``app.main``               ``app.admin_main``
=====================  =========================  ===============================
``root_path``          ``/api/v1``                ``/admin/api/v1``
``cors_origins``       ``cors_origins``           ``admin_cors_origins``
``lifespan``           Redis pools + event hub    nothing to acquire
``readiness_probes``   Redis + DB                 DB only
=====================  =========================  ===============================

This is the same reasoning that produced
:func:`app.exception_handlers.install_error_handlers`, applied to the
rest of the stack: a property that must hold across surfaces belongs in
one function, not in a checklist.
"""

from collections.abc import Awaitable, Callable, Mapping

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import engine
from app.exception_handlers import install_error_handlers
from app.observability import install_observability
from app.rate_limit import limiter, rate_limit_exceeded_handler

API_VERSION = "1.0"

ReadinessProbe = Callable[[FastAPI], Awaitable[str | None]]
"""Checks one dependency: ``None`` when healthy, else a short reason.

Returning a reason rather than raising keeps each probe responsible for
naming the exceptions it expects — :func:`_install_health_routes` never
has to blanket-catch ``Exception`` and call an unrelated bug "not ready".
"""


async def probe_database(_: FastAPI) -> str | None:
    """Readiness probe for PostgreSQL. Shared by both apps."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        return str(exc)
    return None


def create_app(
    *,
    title: str,
    root_path: str,
    cors_origins: list[str],
    lifespan: Callable[[FastAPI], object],
    readiness_probes: Mapping[str, ReadinessProbe],
) -> FastAPI:
    """Build an app with this project's standard HTTP stack.

    Routers are **not** included here — the caller adds its own after
    this returns, which keeps the router set the one thing each app
    obviously owns.

    Args:
        title: OpenAPI title.
        root_path: Path prefix the reverse proxy strips before us.
        cors_origins: Explicit allowlist. Never ``["*"]`` — we send
            ``allow_credentials=True`` and browsers reject the pair.
        lifespan: The app's own startup/shutdown context manager.
        readiness_probes: ``{name: probe}`` behind ``/health/ready``;
            the name prefixes the reason in the 503 body. Iteration
            order is the reporting order.
    """
    app = FastAPI(
        title=title,
        version=API_VERSION,
        root_path=root_path,
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    install_error_handlers(app)

    # SlowAPIMiddleware makes ``limiter.default_limits`` an actual global
    # floor. Without it slowapi only enforces limits on endpoints carrying
    # an explicit ``@limiter.limit`` decorator — every other route is
    # completely unthrottled (the default 200/min is inert). Added BEFORE
    # CORS so CORS wraps it: a 429 response still gets its
    # ``Access-Control-Allow-Origin`` headers, otherwise the browser would
    # surface an opaque network error instead of the rate-limit JSON.
    app.add_middleware(SlowAPIMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # So the browser can read the correlation id off a response and
        # put it in a bug report / error toast.
        expose_headers=["X-Request-ID"],
    )

    # Sentry + Prometheus /metrics + the request-id middleware. Installed
    # after CORS so the request-id layer is outermost (correlation id
    # bound before anything inner logs); CORS stays inside it, still
    # wrapping the routes so error responses keep their CORS headers.
    install_observability(app)

    _install_health_routes(app, readiness_probes)
    return app


def _install_health_routes(
    app: FastAPI, readiness_probes: Mapping[str, ReadinessProbe]
) -> None:
    """Register ``/health/live`` and ``/health/ready``.

    Both are rate-limit exempt: K8s polls them every few seconds per
    node, so a per-IP 200/min cap would 429 the probe, K8s would mark the
    pod unhealthy, and it would be killed for being healthy.
    """

    @app.get("/health/live")
    @limiter.exempt
    async def health_live():
        """Liveness — process is up. Always 200 unless the loop is dead."""
        return {"status": "ok"}

    @app.get("/health/ready")
    @limiter.exempt
    async def health_ready():
        """Readiness — every configured dependency is reachable.

        503 when one is down so K8s stops routing traffic; the pod keeps
        running (liveness still 200) so it recovers when the dependency
        returns, without a restart cycle.
        """
        errors = [
            f"{name}: {reason}"
            for name, probe in readiness_probes.items()
            if (reason := await probe(app)) is not None
        ]
        if errors:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"status": "not ready", "errors": errors},
            )
        return {"status": "ok"}
