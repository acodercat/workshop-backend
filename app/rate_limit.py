"""Per-identity rate limiting via slowapi.

Endpoints opt in by adding ``@limiter.limit(...)`` and accepting a
``request: Request`` parameter (slowapi reads the key from the request).
Limits live in :mod:`config` so they can be tuned per-environment.

The key is the **authenticated principal** when one is present, falling
back to the **client IP** for anonymous (pre-login) endpoints — see
:func:`_rate_limit_key`. Keying authenticated traffic on the principal
(not the IP) is the standard fix for NAT/CGNAT: hundreds of real users
behind one corporate / mobile-carrier egress IP otherwise share a single
bucket and throttle each other. A principal is a customer (``user:{id}``)
or a console operator (``staff:{id}``), stamped by
:func:`set_rate_limit_principal`.

That holds for **decorated** limits, which slowapi checks inside the
endpoint, after the auth dependency has run. The global floor
(``default_limits``, applied by the middleware in :mod:`app.asgi`) runs
before any dependency and so always keys on the IP. It is sized as a
floor accordingly — every limit that matters is a decorator.

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

from typing import Literal

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


def set_rate_limit_principal(request: Request, kind: Literal["user", "staff"], id_: object) -> None:
    """Record the authenticated identity slowapi should key on.

    Called by the authentication dependencies in :mod:`app.auth` once a
    Bearer token is validated. ``kind`` namespaces the id — ``"user"``
    for customers, ``"staff"`` for console operators — so the two id
    spaces cannot collide in the shared Redis keyspace, and neither can
    collide with the ``ip:`` fallback. The ``Literal`` is the registry of
    principal kinds: a new surface extends it here, visibly, rather than
    minting an ad-hoc prefix at its call site.

    Namespacing happens **here**, once. Callers pass their kind and their
    raw id and never pre-format the key themselves; two places building
    the same string is how a key ends up prefixed twice.
    """
    request.state.rate_limit_principal = f"{kind}:{id_}"


def _rate_limit_key(request: Request) -> str:
    """Rate-limit key: the authenticated principal if known, else the IP.

    :func:`set_rate_limit_principal` stamps the principal once a Bearer
    token is validated, and FastAPI resolves that dependency before a
    decorated limit evaluates this key — so such a request keys on
    ``user:{id}`` / ``staff:{id}`` and is immune to NAT-shared IPs, while
    a pre-login request, and the middleware's global floor, key on
    ``ip:{addr}``.
    """
    principal = getattr(request.state, "rate_limit_principal", None)
    if principal is not None:
        return principal
    return f"ip:{_client_ip(request)}"


limiter = Limiter(
    key_func=_rate_limit_key,
    default_limits=[settings.rate_limit_default],
    storage_uri=settings.redis_url,
    in_memory_fallback_enabled=True,
    swallow_errors=True,
)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
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
