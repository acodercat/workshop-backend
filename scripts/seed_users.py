"""One-shot user seeder.

Runs against the configured ``db_uri``; idempotent — skips any
username that already exists. Login uses the ``username`` column as
the OAuth2 ``username`` (see :mod:`app.api.endpoints.auth`), so the
value here is what users type at sign-in. Seeded accounts have no
email (legacy bypass — public registration goes through
``POST /users`` and the email-verification flow).

Usage:
    uv run python -m scripts.seed_users
"""

import asyncio

import psycopg

from app.auth import get_password_hash
from config import settings

USERS = [
    ("david", "david20260511"),
    # ("andy", "andy123"),
    # ("zhou", "zhou123"),
    # ("jun", "jun123"),
    # ("yuting", "yuting123"),
    # ("xuchang", "xuchang123"),
]


async def main() -> None:
    psycopg_uri = settings.db_uri.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(psycopg_uri) as conn, conn.cursor() as cur:
        for username, password in USERS:
            cur.execute('SELECT id FROM "user" WHERE username = %s', (username,))
            if cur.fetchone():
                print(f"skip  {username} (already exists)")
                continue
            # ``token_version`` is a Python-side default on the ORM
            # model; raw INSERT must pass it explicitly because the
            # DB column has NOT NULL with no server default. ``status``
            # is set to ``ACTIVE`` explicitly so seeded dev accounts
            # bypass the ``PENDING`` server default — they need to log
            # in immediately. ``email`` left NULL — these are dev
            # seeds, not registered users.
            cur.execute(
                'INSERT INTO "user" '
                "(name, username, password_hash, token_version, status) "
                "VALUES (%s, %s, %s, 0, 'ACTIVE') RETURNING id",
                (username, username, await get_password_hash(password)),
            )
            new_id = cur.fetchone()[0]
            print(f"added {username} (id={new_id})")
        conn.commit()


if __name__ == "__main__":
    asyncio.run(main())
