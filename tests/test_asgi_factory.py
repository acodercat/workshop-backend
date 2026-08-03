"""``create_app`` — the HTTP stack both surfaces share.

The factory is the one place the error envelope, the 401 challenge, the
request-id layer and the health probes are wired, for the public API and
the admin console alike. Testing it once here is testing both apps'
plumbing — which is the point of it being a factory.

One app is built at module scope: ``install_observability`` registers
Prometheus metrics against the process-global registry, so building an
app per test would re-register them.

asyncio_mode = "auto" applies to async tests; these drive the app through
``TestClient`` and are synchronous.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import StaffAuthenticationError, StaffForbiddenError
from config import settings

# Building an app initializes Sentry if a DSN is configured; a unit-test
# process must never report there. Cleared before the factory import path
# runs — no other offline test reads this setting.
settings.sentry_dsn = ""

from app.asgi import create_app  # noqa: E402  (after the sentry guard)
from app.models.admin_audit_log import AdminAuditLog  # noqa: E402
from app.observability import REQUEST_ID_MAX_LENGTH  # noqa: E402

_db_reason: list[str | None] = [None]
"""What the fake DB probe reports; tests flip it to simulate an outage."""


async def _fake_db_probe(_: FastAPI) -> str | None:
    return _db_reason[0]


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    yield


app = create_app(
    title="Factory Under Test",
    root_path="/test/api/v1",
    cors_origins=["http://localhost:9999"],
    lifespan=_noop_lifespan,
    readiness_probes={"db": _fake_db_probe},
)


@app.get("/boom-unauthenticated")
async def boom_unauthenticated():
    raise StaffAuthenticationError("who are you")


@app.get("/boom-forbidden")
async def boom_forbidden():
    raise StaffForbiddenError("not with that role")


client = TestClient(app)


# --- health probes ------------------------------------------------------


def test_liveness_is_unconditional() -> None:
    assert client.get("/health/live").status_code == 200


def test_readiness_passes_when_every_probe_is_quiet() -> None:
    _db_reason[0] = None

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_names_the_failing_dependency() -> None:
    _db_reason[0] = "connection refused"
    try:
        response = client.get("/health/ready")
    finally:
        _db_reason[0] = None

    assert response.status_code == 503
    # Through the shared HTTPException handler: the dict detail rides in
    # ``details``, prefixed with the probe's name so the 503 says *what*
    # is down, not just that something is.
    details = response.json()["error"]["details"]
    assert details["errors"] == ["db: connection refused"]


# --- the error envelope, end to end -------------------------------------


def test_app_error_serializes_into_the_envelope() -> None:
    response = client.get("/boom-forbidden")

    assert response.status_code == 403
    assert response.json() == {
        "error": {"type": "staff_forbidden", "message": "not with that role"}
    }


def test_a_401_carries_the_bearer_challenge() -> None:
    # RFC 7235: every 401 must offer a WWW-Authenticate challenge. The
    # handler adds it centrally, so an ``AppError``-raising auth path is
    # as conformant as the ``HTTPException``-raising ones.
    response = client.get("/boom-unauthenticated")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_403_carries_no_challenge() -> None:
    assert "WWW-Authenticate" not in client.get("/boom-forbidden").headers


# --- request-id correlation ---------------------------------------------


def test_every_response_carries_a_request_id() -> None:
    response = client.get("/health/live")

    assert response.headers.get("X-Request-ID")


def test_a_sane_inbound_request_id_is_reused() -> None:
    response = client.get(
        "/health/live", headers={"X-Request-ID": "trace-abc.123"}
    )

    assert response.headers["X-Request-ID"] == "trace-abc.123"


def test_a_garbage_inbound_request_id_is_replaced() -> None:
    # An id that fails the sanity pattern would poison every log line it
    # gets bound to; the middleware mints a fresh one instead.
    response = client.get(
        "/health/live", headers={"X-Request-ID": "bad id with spaces!"}
    )

    minted = response.headers["X-Request-ID"]
    assert minted and minted != "bad id with spaces!"


def test_the_longest_storable_request_id_is_reused() -> None:
    longest = "t" * REQUEST_ID_MAX_LENGTH
    response = client.get("/health/live", headers={"X-Request-ID": longest})

    assert response.headers["X-Request-ID"] == longest


def test_a_request_id_too_long_to_store_is_replaced() -> None:
    # Replaced, not truncated: a truncated id matches nothing in the
    # gateway's own logs, which is worse than an honestly minted one.
    too_long = "t" * (REQUEST_ID_MAX_LENGTH + 1)
    response = client.get("/health/live", headers={"X-Request-ID": too_long})

    minted = response.headers["X-Request-ID"]
    assert minted and minted != too_long


def test_the_acceptor_cap_is_the_audit_column_width() -> None:
    # ``admin_audit_log.request_id`` stores the id in the same transaction
    # as the action it describes, so an id the acceptor admits but the
    # column cannot hold would abort that action. The two live in
    # different layers on purpose (the middleware owns the contract, the
    # model stays free of app-layer imports) — this is what ties them.
    assert AdminAuditLog.request_id.type.length == REQUEST_ID_MAX_LENGTH
