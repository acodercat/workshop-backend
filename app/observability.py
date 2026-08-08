"""Production observability: request-id correlation, Sentry, Prometheus.

All three are additive and safe to run with their backends absent —
Sentry is a no-op until a DSN is configured, the ``/metrics`` endpoint
has no external dependency, and request-id correlation is pure
in-process. Wire everything onto the app with a single
:func:`install_observability` call.
"""

import logging
import re
import uuid

from loguru import logger

from config import settings

stdlib_logger = logging.getLogger(__name__)

# The longest correlation id this backend accepts — and therefore the
# longest it ever has to store. ``admin_audit_log.request_id`` persists
# the id at exactly this width, and that row commits in the same
# transaction as the mutation it describes, so an id that fit the
# acceptor but not the column would abort the very action being audited.
# Our own minted ids are 32 hex chars; 64 leaves room for any sane
# gateway id. An inbound id past the cap is not truncated — it is
# replaced, like any other value that fails the pattern.
REQUEST_ID_MAX_LENGTH = 64

# An inbound ``X-Request-ID`` is only reused if it looks like a sane
# correlation token. Rejecting anything else (then minting our own)
# stops a client from forging log lines or smuggling junk into the
# echoed response header via a crafted value.
_REQUEST_ID_RE = re.compile(rf"\A[A-Za-z0-9_.-]{{1,{REQUEST_ID_MAX_LENGTH}}}\Z")


class RequestIDMiddleware:
    """Tag each HTTP request with a correlation id.

    Reuses an inbound ``X-Request-ID`` (set by an upstream proxy /
    gateway) when present, otherwise mints one. Binds it into the loguru
    context so every log line emitted while handling the request —
    including stdlib logs routed through the intercept handler — carries
    ``request_id``, and echoes it back in the response header so a client
    error report can be matched to server logs. The id is also placed on
    the ASGI scope state (``request.state.request_id``) for code that
    stores it rather than logs it.

    Implemented as raw ASGI rather than ``BaseHTTPMiddleware`` because the
    latter buffers the response body, which breaks the SSE streaming
    endpoints. This wrapper only touches the response *start* message, so
    streamed bodies pass through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = dict(scope["headers"]).get(b"x-request-id")
        candidate = inbound.decode("latin-1") if inbound else ""
        request_id = candidate if _REQUEST_ID_RE.match(candidate) else uuid.uuid4().hex

        # Also expose it on the ASGI scope state, which is what backs
        # ``request.state``. The loguru contextvar above covers *logging*,
        # but code that needs to persist the correlation id alongside a
        # row — ``admin_audit_log.request_id`` — needs to read it as a
        # value. Additive: nothing else inspects this key.
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("latin-1"))
                )
            await send(message)

        with logger.contextualize(request_id=request_id):
            await self.app(scope, receive, send_with_id)


def _init_sentry() -> None:
    """Initialise Sentry if a DSN is configured; otherwise a no-op.

    ``send_default_pii`` stays off: this API carries auth tokens and
    user content, none of which should leave the box by default. Errors
    are reported via explicit ``capture_exception`` in the 500-class
    exception handlers (the catch-all handler turns unhandled exceptions
    into responses before Sentry's ASGI integration could see them).
    """
    if not settings.sentry_dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment or None,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
    )
    stdlib_logger.info(
        "Sentry error tracking enabled (environment=%s)",
        settings.sentry_environment or "-",
    )


def install_observability(app) -> None:
    """Wire request-id correlation, Sentry, and ``/metrics`` onto ``app``.

    The request-id middleware is added last so it sits outermost and the
    correlation id is bound before any inner layer (CORS, rate-limit,
    routes) gets a chance to log.
    """
    _init_sentry()

    if settings.metrics_enabled:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator(
            excluded_handlers=["/metrics", "/health/live", "/health/ready"],
        ).instrument(app).expose(
            app, endpoint="/metrics", include_in_schema=False,
        )

    app.add_middleware(RequestIDMiddleware)
