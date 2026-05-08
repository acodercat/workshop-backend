"""Per-IP rate limiting via slowapi.

Endpoints opt in by adding ``@limiter.limit(...)`` and accepting a
``request: Request`` parameter (slowapi reads the IP from the request).
Limits live in :mod:`config` so they can be tuned per-environment.
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config import settings

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default],
)


async def rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """Emit the project's nested ``{error: {type, message}}`` envelope
    on 429 — keeps slowapi-driven rate limits indistinguishable from
    application-driven errors at the wire level."""
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "type": "rate_limited",
                "message": f"Rate limit exceeded: {exc.detail}",
            },
        },
        headers={"Retry-After": "60"},
    )
