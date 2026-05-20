"""One-shot user seeder.

Runs against the configured ``db_uri``; idempotent — skips any
username that already exists. Login uses the ``username`` column as
the OAuth2 ``username`` (see :mod:`app.api.endpoints.auth`), so the
value here is what users type at sign-in. Seeded accounts have no
email (legacy bypass — public registration goes through
``POST /users`` and the email-verification flow).

Each seeded user gets a personal organization auto-created and a
:data:`WELCOME_CREDITS`-credit welcome grant, same as the regular
registration flow (see :mod:`app.services.auth.register`). Done
here in raw SQL because this script bypasses the service layer to
keep psycopg-only deps.

Usage:
    uv run python -m scripts.seed_users
"""

import asyncio

import psycopg
from psycopg.types.json import Jsonb

from app.auth import get_password_hash
from config import settings

USERS = [
    # ("tianlan", "tianlan20260518"),
    ("sunwei", "sunwei20260520"),
    # ("andy", "andy123"),
    # ("zhou", "zhou123"),
    # ("jun", "jun123"),
    # ("yuting", "yuting123"),
    # ("xuchang", "xuchang123"),
]

# Whole credits granted to every freshly seeded user. Matches the
# registration default (``settings.credit_welcome_grant_credits``).
# Kept as a script-local constant so dev seeding isn't coupled to
# whatever an admin might change the production setting to.
WELCOME_CREDITS = 20000000
_MICRO_PER_CREDIT = 1_000_000


def _create_user_with_personal_org(
    cur: psycopg.Cursor, *, username: str, password_hash: str,
) -> int:
    """INSERT a user + their personal org + OWNER membership, link
    them, return the new user id. All in the caller's transaction.

    Mirrors :func:`app.services.auth.register.register_user`'s
    user+org creation, but in raw SQL so this script doesn't need
    to import the SQLAlchemy session layer.
    """
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
        "INSERT INTO organization_member (organization_id, user_id, role) "
        "VALUES (%s, %s, 'OWNER')",
        (organization_id, user_id),
    )
    cur.execute(
        'UPDATE "user" SET organization_id = %s WHERE id = %s',
        (organization_id, user_id),
    )
    return user_id


def _grant_welcome_credits(cur: psycopg.Cursor, *, user_id: int) -> None:
    """Mirror the registration welcome grant in raw SQL.

    Inserts a ``TOPUP`` ledger row and bumps ``user.credit_balance``
    in the caller's transaction. The ``welcome-{user_id}``
    idempotency key is what tags the row as ``WELCOME_GRANT`` in
    the user-facing transaction view (see
    :mod:`app.services.credits.transactions`).
    """
    amount_micro = WELCOME_CREDITS * _MICRO_PER_CREDIT
    cur.execute(
        "INSERT INTO credit_ledger "
        "(user_id, amount, kind, reference, idempotency_key) "
        "VALUES (%s, %s, 'TOPUP', %s, %s)",
        (
            user_id,
            amount_micro,
            Jsonb({"reason": "welcome grant", "admin_id": None, "source": "admin"}),
            f"welcome-{user_id}",
        ),
    )
    cur.execute(
        'UPDATE "user" SET credit_balance = credit_balance + %s WHERE id = %s',
        (amount_micro, user_id),
    )


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
            new_id = _create_user_with_personal_org(
                cur,
                username=username,
                password_hash=await get_password_hash(password),
            )
            _grant_welcome_credits(cur, user_id=new_id)
            print(f"added {username} (id={new_id}, +{WELCOME_CREDITS} credits)")
        conn.commit()


if __name__ == "__main__":
    asyncio.run(main())
