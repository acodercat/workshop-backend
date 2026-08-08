"""Console sign-in — the refused half.

The success path stamps ``last_login_at`` and writes the ``staff.login``
audit row; what these tests pin is the half that used to be invisible: a
refused attempt answers the caller uniformly and tells the log the truth.
The two must diverge — the response so a former operator cannot confirm
their account still exists, the log so incident review can see the
console being probed at all.

The endpoint is exercised through a real app (the slowapi decorator wants
a real ``Request``), with the two seams mocked where every offline test
mocks them: the DB dependency is overridden and ``authenticate_staff`` is
patched at the module that imported it. ``TestClient`` runs its own loop,
so the test stays synchronous — same rule as ``test_asgi_factory``.
"""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import auth as admin_auth
from app.api.dependencies import get_db
from app.exception_handlers import install_error_handlers
from app.models.staff import Staff, StaffStatus
from app.rate_limit import limiter


@pytest.fixture(autouse=True)
def _rate_limit_off(monkeypatch):
    # The limiter counts in Redis and has its own tests; here it would
    # only make the second refused attempt a 429 instead of a 401.
    monkeypatch.setattr(limiter, "enabled", False)


def _client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_auth.router)
    app.dependency_overrides[get_db] = lambda: object()
    return TestClient(app)


def _disabled_account() -> Staff:
    return Staff(
        id=3,
        username="mallory",
        password_hash="x",
        name="Mallory",
        status=StaffStatus.DISABLED,
        token_version=0,
    )


def _sign_in(client: TestClient):
    return client.post(
        "/auth/access-tokens",
        json={"username": "mallory", "password": "wrong"},
    )


def test_a_refused_attempt_is_uniform_outside_and_specific_inside(
    monkeypatch, caplog
) -> None:
    client = _client()
    refusals = []

    with caplog.at_level(logging.WARNING, logger="app.api.admin.auth"):
        # Unknown account (or wrong password — authenticate_staff folds
        # the two on purpose), then a real-but-disabled account.
        async def _nobody(db, username, password):
            return None

        monkeypatch.setattr(admin_auth, "authenticate_staff", _nobody)
        refusals.append(_sign_in(client))

        async def _disabled(db, username, password):
            return _disabled_account()

        monkeypatch.setattr(admin_auth, "authenticate_staff", _disabled)
        refusals.append(_sign_in(client))

    # Outside: byte-for-byte the same refusal, so probing cannot tell a
    # dead password from a disabled account.
    assert [r.status_code for r in refusals] == [401, 401]
    assert refusals[0].json() == refusals[1].json()
    assert refusals[0].json()["error"]["type"] == "staff_invalid_credentials"

    # Inside: one warning per attempt, naming the target and the cause
    # the response withheld. Brute-forcing the console's front door must
    # not be silent — this is the trace incident review greps for.
    lines = [
        r.getMessage() for r in caplog.records if "sign-in refused" in r.getMessage()
    ]
    assert len(lines) == 2
    assert all("'mallory'" in line for line in lines)
    assert "unknown account or wrong password" in lines[0]
    assert "account disabled" in lines[1]
