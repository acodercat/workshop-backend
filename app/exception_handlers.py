"""HTTP error handlers — convert every exception path into the
project's Stripe-style envelope.

    {"error": {"type": "<snake_case>", "message": "<english>",
               "details": <optional structured payload>}}

Five handlers cover the surface:

- :func:`app_error_handler` — typed :class:`~app.errors.AppError` raises
  from service / endpoint code.
- :func:`http_exception_handler` — bare :class:`HTTPException` from
  framework (404 routes, FastAPI's auth dependencies) and from
  endpoints that need a quick exit without inventing an
  :class:`AppError` subclass; ``type`` is derived from the status
  code's :class:`HTTPStatus` phrase.
- :func:`integrity_error_handler` and
  :func:`sqlalchemy_exception_handler` — DB-layer escapes that bypass
  the typed hierarchy.
- :func:`validation_exception_handler` — Pydantic / FastAPI request-body
  validation failures; emits ``error.details`` with the per-field
  breakdown.
- :func:`general_exception_handler` — last-resort 500 catch, answered by
  :class:`UnexpectedErrorMiddleware` rather than registered as a handler.

:func:`install_error_handlers` registers all of them on an app in one
call. Every ASGI entrypoint in the project (the public API, the admin
console) uses it, so the error envelope is identical across surfaces and
adding a handler can't leave one app behind.
"""

import logging
from http import HTTPStatus
from typing import Any

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import AppError

logger = logging.getLogger(__name__)


def _error_envelope(
    *,
    type_: str,
    message: str,
    details: object | None = None,
) -> dict[str, Any]:
    """Build the canonical Stripe-style error envelope ::

        {"error": {"type": ..., "message": ..., "details": ...}}

    ``details`` is omitted from the wire when ``None`` so callers
    don't litter responses with explicit nulls."""
    error: dict[str, Any] = {"type": type_, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


def _phrase_to_type(code: int) -> str:
    """Map an HTTP status code to a ``snake_case`` reason string via
    :class:`HTTPStatus`'s standard reason phrase. ``404`` →
    ``"not_found"``; ``401`` → ``"unauthorized"``. Falls back to
    ``"http_error"`` for codes outside :class:`HTTPStatus`."""
    try:
        return HTTPStatus(code).phrase.lower().replace(" ", "_")
    except ValueError:
        return "http_error"


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    # RFC 7235 requires ``WWW-Authenticate`` on every 401. Bearer is the
    # only scheme any surface here speaks, so the challenge is emitted
    # centrally rather than remembered per error class — a 401 ``AppError``
    # (``StaffAuthenticationError``, ``StaffCredentialsError``) gets the
    # same header the ``HTTPException``-raising auth paths already send.
    headers = (
        {"WWW-Authenticate": "Bearer"} if exc.status_code == status.HTTP_401_UNAUTHORIZED else None
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_envelope(
            type_=exc.error_type,
            message=exc.message,
            details=exc.details,
        ),
        headers=headers,
    )


async def integrity_error_handler(
    request: Request,
    exc: IntegrityError,
) -> JSONResponse:
    logger.warning("Integrity error: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=_error_envelope(
            type_="conflict",
            message="The request violates a database constraint.",
        ),
    )


async def sqlalchemy_exception_handler(
    request: Request,
    exc: SQLAlchemyError,
) -> JSONResponse:
    logger.error("Database error: %s", exc, exc_info=True)
    # 5xx — a real server fault. Report to Sentry (no-op without a DSN).
    sentry_sdk.capture_exception(exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_envelope(
            type_="database_error",
            message="An error occurred while processing your request.",
        ),
    )


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Serialize bare ``HTTPException`` into the envelope.

    ``exc.detail`` is treated two ways:

    - **dict** — caller passed a structured payload (currently the
      readiness probe in ``app/asgi.py`` returns ``{"status": "...",
      "errors": [...]}``); pass it through under ``details`` and
      synthesize ``message`` from the status phrase.
    - **str / anything else** — used as ``message`` directly.

    ``type`` is derived from the status code so call sites that raise
    plain ``HTTPException`` get a meaningful reason code automatically
    (``404`` → ``"not_found"``, ``401`` → ``"unauthorized"``, etc.)
    without touching the individual sites.
    """
    type_ = _phrase_to_type(exc.status_code)
    if isinstance(exc.detail, dict):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(
                type_=type_,
                message=HTTPStatus(exc.status_code).phrase,
                details=exc.detail,
            ),
            headers=exc.headers,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_envelope(type_=type_, message=str(exc.detail)),
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Pydantic / FastAPI request-body validation failure.

    Per-field breakdown lives at ``error.details`` (a flat list, not
    the previous ``{errors: [...]}`` nested-wrapper layer). The
    frontend's generic error reader pulls ``error.message`` for
    toasts; forms that want field-level errors read ``error.details``.
    """
    details = [
        {
            "location": list(err["loc"]),
            "message": err["msg"],
            "type": err["type"],
            **({"context": _jsonable_ctx(err["ctx"])} if "ctx" in err else {}),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=_error_envelope(
            type_="validation_error",
            message="Request body failed validation.",
            details=details,
        ),
    )


def _jsonable_ctx(ctx: dict) -> dict:
    """Make a pydantic error ``ctx`` JSON-safe.

    Constraint contexts are plain scalars (``{"ge": 1}``) and pass
    through untouched — but a custom ``field_validator`` that raises
    ``ValueError`` arrives as ``{"error": ValueError(...)}``, a live
    exception object that would crash serialization and turn the 422
    into a 500. Stringify anything that isn't already JSON-native.
    """
    return {
        key: value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
        for key, value in ctx.items()
    }


async def general_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    logger.exception("Unexpected error: %s", exc)
    # Last-resort 500 — always report (no-op without a Sentry DSN).
    sentry_sdk.capture_exception(exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_envelope(
            type_="internal_server_error",
            message="An unexpected error occurred.",
        ),
    )


class UnexpectedErrorMiddleware:
    """Answer an exception no handler took, from inside the other middleware.

    Starlette runs an ``Exception`` handler in its outermost middleware, so
    that 500 would carry neither the request id nor CORS headers, and a
    browser would report a network error instead of the envelope. A
    response already started has nothing left to answer with, and its
    exception propagates.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            if started:
                raise
            response = await general_exception_handler(Request(scope), exc)
            await response(scope, receive, send)


def install_error_handlers(app: FastAPI) -> None:
    """Register every handler above on ``app``, and the middleware for what
    none of them takes.

    Order of registration does not matter to the handlers — Starlette
    dispatches on the exception class, resolving to the most specific
    registered base — but the set does: omitting any one would let that
    path fall through to the bare 500. The middleware must be added before
    any other, so that it runs innermost.
    """
    # Each ``ignore`` covers the same thing: Starlette types the handler as
    # taking ``Exception``, while every handler below narrows to the class it
    # is registered for. Starlette dispatches on that class, so the narrowing
    # is exactly right at runtime — it is the signature that cannot say so.
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_middleware(UnexpectedErrorMiddleware)
