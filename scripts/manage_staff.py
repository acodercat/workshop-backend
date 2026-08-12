"""Provision and maintain admin-console staff accounts.

The console has no self-registration and no invite flow: staff accounts
are created here, by someone who already holds database credentials. That
is the same admin-gating posture as ``create_invite_code`` and
``topup_credits`` — process auth is DB credentials.

Password rotation lives here too, rather than only in the console, so a
fully locked-out console is recoverable without a working account.

The account rules themselves are **not** duplicated here: this drives
:mod:`app.services.admin.staff`, the same module the console's endpoints
call, so the password floor and the sign-out-on-reset behaviour cannot
drift between the two entry points. What the script does *not* get is an
audit row — ``admin_audit_log.staff_id`` is ``NOT NULL`` and a script run
has no operator to attribute the action to. That is the honest record:
these runs are authorized by database credentials, and the shell they
were typed into is where they are traceable.

The password is read from the ``STAFF_PASSWORD`` environment variable
rather than an argument, so it never lands in shell history or in the
process list where ``ps`` would show it.

Usage:
    # Create an operator account
    STAFF_PASSWORD='...' uv run python -m scripts.manage_staff \\
        --create --username=alice --name="Alice"

    # Rotate a password (also signs that account out everywhere)
    STAFF_PASSWORD='...' uv run python -m scripts.manage_staff \\
        --reset-password --username=alice
"""

import argparse
import asyncio
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.errors import StaffUsernameTakenError
from app.models.staff import Staff
from app.services.admin import staff as staff_service


async def _create(db: AsyncSession, *, username: str, name: str, password: str) -> int:
    try:
        account = await staff_service.create(db, username=username, name=name, password=password)
    except StaffUsernameTakenError:
        print(f"Staff {username!r} already exists.", file=sys.stderr)
        return 1

    await db.commit()
    print(f"OK  created staff {account.username}")
    return 0


async def _reset_password(db: AsyncSession, *, username: str, password: str) -> int:
    account = await db.scalar(select(Staff).where(Staff.username == username))
    if account is None:
        print(f"No staff account named {username!r}.", file=sys.stderr)
        return 1

    # The service rotates the hash *and* signs every existing session out
    # — the whole point of a reset after a suspected leak.
    await staff_service.set_password(db, account, password)
    await db.commit()
    print(f"OK  reset password for {username}")
    return 0


async def main(args: argparse.Namespace) -> int:
    password = os.environ.get("STAFF_PASSWORD", "")
    if len(password) < staff_service.MIN_PASSWORD_CHARS:
        print(
            f"STAFF_PASSWORD must be set and at least "
            f"{staff_service.MIN_PASSWORD_CHARS} characters.",
            file=sys.stderr,
        )
        return 1

    async with AsyncSessionLocal() as db:
        if args.create:
            return await _create(db, username=args.username, name=args.name, password=password)
        return await _reset_password(
            db,
            username=args.username,
            password=password,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--create",
        action="store_true",
        help="Create a new staff account.",
    )
    action.add_argument(
        "--reset-password",
        action="store_true",
        help="Rotate an existing account's password and sign it out everywhere.",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="Login identifier for the console.",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Human name shown in the console. Required with --create.",
    )
    args = parser.parse_args()
    if args.create and not args.name:
        parser.error("--create requires --name")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
