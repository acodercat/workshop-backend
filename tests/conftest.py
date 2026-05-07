"""Shared fixtures for backend tests.

E2E tests are opt-in via ``RUN_E2E_TESTS=1`` because they require the
full pipeline running (backend + worker + Redis + Postgres + sandbox)
and burn real LLM tokens. Apply :data:`requires_e2e` to gate them.
"""

import os
import secrets
from typing import AsyncIterator, Iterator

import bcrypt
import httpx
import psycopg
import pytest

from config import settings


# Gate: skip every test marked with this unless the operator explicitly
# opted in. Mirrors the axon module's ``RUN_INTEGRATION_TESTS`` pattern.
requires_e2e = pytest.mark.skipif(
    os.environ.get("RUN_E2E_TESTS") != "1",
    reason=(
        "Set RUN_E2E_TESTS=1 to run E2E tests against a live backend + "
        "worker + Redis + Postgres + sandbox."
    ),
)


# Backend HTTP base URL. The backend mounts under root_path=/api/v1
# (see app/main.py), and dev defaults to localhost:8005. Override via
# env if a test environment lives elsewhere.
BACKEND_BASE_URL = os.environ.get(
    "BACKEND_BASE_URL", "http://localhost:8005/api/v1",
)


def _psycopg_uri() -> str:
    """Plain libpq URI derived from ``settings.db_uri``.

    The app uses the SQLAlchemy ``postgresql+psycopg://`` /
    ``postgresql+asyncpg://`` driver prefix; psycopg's connect() wants
    the bare ``postgresql://`` form.
    """
    return (
        settings.db_uri
        .replace("postgresql+psycopg://", "postgresql://")
        .replace("postgresql+asyncpg://", "postgresql://")
    )


@pytest.fixture
def test_user() -> Iterator[dict]:
    """Provision a unique throwaway user; tear down everything they own.

    Insert via raw psycopg because the only "create user" path in the
    codebase is :mod:`scripts.seed_users` and the API has no public
    registration endpoint by design. Cleanup cascades:

    - ``DELETE FROM "user"`` cascades to ``session`` (user_id FK) which
      cascades to ``message`` and ``session_turn`` (session_id FK).
    - ``sandbox`` rows have **no** FK to session (application-managed —
      see ``app/models/sandbox.py``), so they need an explicit delete
      keyed off any session that belonged to this user.
    """
    phone = f"e2e-{secrets.token_hex(4)}"
    password = "e2e-test-pw"
    password_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=settings.bcrypt_rounds),
    ).decode("utf-8")

    with psycopg.connect(_psycopg_uri()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO "user" '
                "(name, phone, password_hash, token_version) "
                "VALUES (%s, %s, %s, 0) RETURNING id",
                (phone, phone, password_hash),
            )
            user_id = cur.fetchone()[0]
        conn.commit()

    yield {"id": user_id, "phone": phone, "password": password}

    # Drop sandbox rows first (no CASCADE), then the user (CASCADEs into
    # session → message + session_turn). Both in one transaction so a
    # mid-teardown crash doesn't leave half-deleted state.
    with psycopg.connect(_psycopg_uri()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sandbox WHERE session_id IN "
                '(SELECT id FROM session WHERE user_id = %s)',
                (user_id,),
            )
            cur.execute('DELETE FROM "user" WHERE id = %s', (user_id,))
        conn.commit()


@pytest.fixture
async def http_client(test_user: dict) -> AsyncIterator[httpx.AsyncClient]:
    """Authenticated httpx client bound to the backend.

    Logs in once, sets the Bearer header for the rest of the session.
    Generous default timeout so SSE consumers in individual tests can
    still override per-request via ``client.stream(..., timeout=...)``.
    """
    async with httpx.AsyncClient(
        base_url=BACKEND_BASE_URL, timeout=30.0,
    ) as client:
        resp = await client.post(
            "/auth/access-tokens",
            data={
                "username": test_user["phone"],
                "password": test_user["password"],
            },
        )
        assert resp.status_code == 200, (
            f"login failed: {resp.status_code} {resp.text}"
        )
        token = resp.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client
