"""Shared fixtures for backend tests.

E2E tests are opt-in via ``RUN_E2E_TESTS=1`` because they require the
full pipeline running (backend + worker + Redis + Postgres + sandbox)
and burn real LLM tokens. Apply :data:`requires_e2e` to gate them.
"""

import os
import secrets
from collections.abc import AsyncIterator, Iterator

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
# (see app/public_main.py), and dev defaults to localhost:8005. Override via
# env if a test environment lives elsewhere.
BACKEND_BASE_URL = os.environ.get(
    "BACKEND_BASE_URL",
    "http://localhost:8005/api/v1",
)


def _psycopg_uri() -> str:
    """Plain libpq URI derived from ``settings.db_uri``.

    The app uses the SQLAlchemy ``postgresql+psycopg://`` /
    ``postgresql+asyncpg://`` driver prefix; psycopg's connect() wants
    the bare ``postgresql://`` form.
    """
    return settings.db_uri.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )


@pytest.fixture
def test_user() -> Iterator[dict]:
    """Provision a unique throwaway user; tear down everything they own.

    Insert via raw psycopg because the public registration endpoint
    goes through email verification, which is not test-friendly.
    ``status='ACTIVE'`` overrides the ``PENDING`` server default so the
    seeded account can log in immediately — mirrors
    :mod:`scripts.seed_users`.

    The test body is expected to ``DELETE`` any sessions it created
    via the API; teardown sweeps any stragglers plus the user row.
    The user FK on ``session.user_id`` has no ``ON DELETE CASCADE``,
    so leftover sessions would otherwise block the user delete.
    """
    username = f"e2e-{secrets.token_hex(4)}"
    password = "e2e-test-pw"
    password_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=settings.bcrypt_rounds),
    ).decode("utf-8")

    with psycopg.connect(_psycopg_uri()) as conn:
        with conn.cursor() as cur:
            # User + personal organization + OWNER membership, mirroring
            # the production registration flow (see
            # :mod:`app.services.auth.register`). Done in one transaction
            # so a teardown crash leaves no half-created state.
            cur.execute(
                'INSERT INTO "user" '
                "(name, username, password_hash, token_version, status) "
                "VALUES (%s, %s, %s, 0, 'ACTIVE') RETURNING id",
                (username, username, password_hash),
            )
            user_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO organization (name, kind, owner_user_id) "
                "VALUES ('Personal', 'PERSONAL', %s) RETURNING id",
                (user_id,),
            )
            organization_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO organization_member "
                "(organization_id, user_id, role) "
                "VALUES (%s, %s, 'OWNER')",
                (organization_id, user_id),
            )
            cur.execute(
                'UPDATE "user" SET organization_id = %s WHERE id = %s',
                (organization_id, user_id),
            )
        conn.commit()

    yield {
        "id": user_id,
        "username": username,
        "password": password,
        "organization_id": organization_id,
    }

    # Drop sandbox + session_files (no FK CASCADE from session/user),
    # then any lingering session (CASCADEs into message + session_turn),
    # then unlink + drop the personal org, then the user. One
    # transaction so a teardown crash leaves no half-deleted state.
    # ``organization_member`` cascades on user delete; ``organization``
    # FK is ``ON DELETE RESTRICT`` so we drop the org explicitly after
    # clearing the back-link.
    with psycopg.connect(_psycopg_uri()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sandbox WHERE session_id IN "
                "(SELECT id FROM session WHERE user_id = %s)",
                (user_id,),
            )
            cur.execute(
                "DELETE FROM session_files WHERE session_id IN "
                "(SELECT id FROM session WHERE user_id = %s)",
                (user_id,),
            )
            cur.execute("DELETE FROM session WHERE user_id = %s", (user_id,))
            # Clear the user→org back-link before deleting the org so
            # the RESTRICT FK on ``user.organization_id`` doesn't block.
            cur.execute(
                'UPDATE "user" SET organization_id = NULL WHERE id = %s',
                (user_id,),
            )
            cur.execute(
                "DELETE FROM organization WHERE id = %s",
                (organization_id,),
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
        base_url=BACKEND_BASE_URL,
        timeout=30.0,
    ) as client:
        resp = await client.post(
            "/auth/access-tokens",
            data={
                "username": test_user["username"],
                "password": test_user["password"],
            },
        )
        assert resp.status_code == 200, f"login failed: {resp.status_code} {resp.text}"
        token = resp.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client
