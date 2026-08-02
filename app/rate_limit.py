"""Per-identity rate limiting via slowapi.

Endpoints opt in by adding ``@limiter.limit(...)`` and accepting a
``request: Request`` parameter (slowapi reads the key from the request).
Limits live in :mod:`config` so they can be tuned per-environment.

The key is the **authenticated user** when one is present, falling back
to the **client IP** for anonymous (pre-login) endpoints — see
:func:`_rate_limit_key`. Keying authenticated traffic on the user (not
the IP) is the standard fix for NAT/CGNAT: hundreds of real users behind
one corporate / mobile-carrier egress IP otherwise share a single bucket
and throttle each other.

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


def _client_ip(request: Request) -> str:
    """The client IP, honouring a trusted reverse proxy.

    Default is the socket peer (``get_remote_address``) — correct when
    clients connect directly. Behind a reverse proxy that collapses every
    client onto the proxy's IP (so per-IP limits become one shared bucket),
    set ``trust_proxy_headers`` to key on the first ``X-Forwarded-For`` hop
    instead. **Off by default**: trusting XFF when the deployment is *not*
    behind a proxy that overwrites client-supplied values would let any
    client spoof the key and dodge the limit, so it's opt-in for operators
    who know their proxy sets it.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return get_remote_address(request)


def _rate_limit_key(request: Request) -> str:
    """Rate-limit key: the authenticated user if known, else the client IP.

    :func:`app.auth.authenticate_and_get_user` stamps
    ``request.state.user_id`` once a Bearer token is validated, and
    FastAPI resolves that dependency before slowapi evaluates this key —
    so an authenticated request keys on ``user:{id}`` and is immune to
    NAT-shared IPs, while a pre-login request keys on ``ip:{addr}``. The
    namespaces are prefixed so a user id can never collide with an IP.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id is not None:
        return f"user:{user_id}"
    return f"ip:{_client_ip(request)}"


limiter = Limiter(
    key_func=_rate_limit_key,
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
