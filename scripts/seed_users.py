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
    ("de", "de20260506"),
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
            # is_admin / token_version are Python-side defaults on the
            # ORM model; raw INSERT must pass them explicitly because
            # the DB columns have NOT NULL with no server default.
            cur.execute(
                'INSERT INTO "user" '
                "(name, phone, password_hash, is_admin, token_version) "
                "VALUES (%s, %s, %s, false, 0) RETURNING id",
                (phone, phone, await get_password_hash(password)),
            )
            new_id = cur.fetchone()[0]
            print(f"added {phone} (id={new_id})")
        conn.commit()


if __name__ == "__main__":
    asyncio.run(main())
