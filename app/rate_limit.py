"""Per-IP rate limiting via slowapi.

Endpoints opt in by adding ``@limiter.limit(...)`` and accepting a
``request: Request`` parameter (slowapi reads the IP from the request).
Limits live in :mod:`config` so they can be tuned per-environment.

Counters are stored in **Redis** (``settings.redis_url``), not in
process memory. With multiple uvicorn workers / hosts an in-memory
store would count each process independently, so a ``5/minute`` login
cap would effectively be ``5 × workers`` and would reset on every
restart — useless as brute-force protection. A shared Redis store makes
the limit a true cluster-wide ceiling. If Redis is unreachable the
limiter degrades gracefully rather than failing requests:
``in_memory_fallback_enabled`` keeps counting locally, and
``swallow_errors`` ensures a storage blip never turns a request into a
500.
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
    storage_uri=settings.redis_url,
    in_memory_fallback_enabled=True,
    swallow_errors=True,
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
