"""One-shot user seeder.

Runs against the configured ``db_uri``; idempotent — skips any phone
that already exists. Login uses the ``phone`` column as the OAuth2
``username`` (see ``api/endpoints/session.py`` and the frontend's
``/api/auth/login`` route), so the value here is what users type at
sign-in.

Usage:
    uv run python -m scripts.seed_users
"""

import asyncio

import psycopg

from app.auth import get_password_hash
from config import settings

USERS = [
    ("wang", "wang20260507"),
    # ("andy", "andy123"),
    # ("zhou", "zhou123"),
    # ("jun", "jun123"),
    # ("yuting", "yuting123"),
    # ("xuchang", "xuchang123"),
]


async def main() -> None:
    psycopg_uri = settings.db_uri.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(psycopg_uri) as conn, conn.cursor() as cur:
        for phone, password in USERS:
            cur.execute('SELECT id FROM "user" WHERE phone = %s', (phone,))
            if cur.fetchone():
                print(f"skip  {phone} (already exists)")
                continue
            # ``token_version`` is a Python-side default on the ORM
            # model; raw INSERT must pass it explicitly because the
            # DB column has NOT NULL with no server default. ``status``
            # is set to ``ACTIVE`` explicitly so seeded dev accounts
            # bypass the ``PENDING`` server default — they need to log
            # in immediately.
            cur.execute(
                'INSERT INTO "user" '
                "(name, phone, password_hash, token_version, status) "
                "VALUES (%s, %s, %s, 0, 'ACTIVE') RETURNING id",
                (phone, phone, await get_password_hash(password)),
            )
            new_id = cur.fetchone()[0]
            print(f"added {phone} (id={new_id})")
        conn.commit()


if __name__ == "__main__":
    asyncio.run(main())
