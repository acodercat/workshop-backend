"""One-shot user seeder.

Runs against the configured ``db_uri``; idempotent — skips any
username that already exists. Login uses the ``username`` column as
the OAuth2 ``username`` (see :mod:`app.api.public.auth`), so the
value here is what users type at sign-in. Seeded accounts have no
email (legacy bypass — public registration goes through
``POST /users`` and the email-verification flow).

Each seeded user gets a personal organization auto-created and a
:data:`WELCOME_CREDITS`-credit welcome grant, same as the regular
registration flow (see :mod:`app.services.auth.register`). User/org
creation stays raw SQL because this is a seed script; credit writes
go through :mod:`app.services.credits` so the ledger/balance invariant
uses the same path as production top-ups.

Usage:
    uv run python -m scripts.seed_users
"""

import asyncio

import psycopg

from app.auth import get_password_hash
from app.database import AsyncSessionLocal
from app.services import credits
from app.services.credits import TopupAlreadyApplied
from app.services.credits.rates import micro_from_credits
from config import settings

USERS = [
    # ("tianlan", "tianlan20260518"),
    ("libinbin", "bidtest20260731"),
    # ("vip202612", "20260324qsax"),
    # ("vip202613", "20260981ajsi"),
    # ("vip202614", "20260424q1sc"),
    # ("vip202615", "20260524azc"),
    # ("vip202616", "20260624uyc"),
    # ("vip202617", "20260724ar5"),
    # ("vip202618", "20260824qqf7"),
    # ("vip202619", "20260924azz1"),
    # ("vip202620", "20260424q1sc"),
    # ("liyangmin", "liyangmin20260604"),
    # ("wangjian", "wangjian20260604"),
    # ("ligang", "ligang20260604"),
    # ("andy", "andy123"),
    # ("zhou", "zhou123"),
    # ("jun", "jun123"),
    # ("yuting", "yuting123"),
    # ("xuchang", "xuchang123"),
]


WELCOME_CREDITS = 100000


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


async def _grant_welcome_credits(*, user_id: int) -> None:
    """Mirror the registration welcome grant through the credit service.

    The ``welcome-{user_id}`` idempotency key is what tags the row as
    ``WELCOME_GRANT`` in the user-facing transaction view. Using
    ``credits.apply_topup`` keeps seeded accounts on the same
    ledger/balance write path as production registration.
    """
    if WELCOME_CREDITS <= 0:
        return
    async with AsyncSessionLocal() as db:
        try:
            await credits.apply_topup(
                db,
                user_id=user_id,
                amount_micro=micro_from_credits(WELCOME_CREDITS),
                idempotency_key=f"welcome-{user_id}",
                reason="welcome grant",
            )
            await db.commit()
        except TopupAlreadyApplied:
            pass


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
            conn.commit()
            await _grant_welcome_credits(user_id=new_id)
            print(f"added {username} (id={new_id}, +{WELCOME_CREDITS} credits)")


if __name__ == "__main__":
    asyncio.run(main())
