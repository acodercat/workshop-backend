"""Mint a SYSTEM invite code — the cold-start path.

Registration is invite-gated, so the first users (who have no
referrer) redeem an operator-issued ``SYSTEM`` code. Per-user
``REFERRAL`` codes are minted automatically at registration; this
script is only for the SYSTEM seed.

Same admin-gating posture as ``topup_credits`` — process auth is DB
credentials.

Usage:
    # Unlimited, never-expiring beta code
    uv run python -m scripts.create_invite_code --note="beta wave 1"

    # Capped + time-boxed
    uv run python -m scripts.create_invite_code \\
        --max-uses=50 --expires-days=30 --note="launch promo"
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from app.database import AsyncSessionLocal
from app.models.invite_code import InviteCodeOrigin
from app.services.auth import create_invite_code


async def main(args: argparse.Namespace) -> int:
    expires_at = (
        datetime.now(UTC) + timedelta(days=args.expires_days)
        if args.expires_days is not None
        else None
    )
    async with AsyncSessionLocal() as db:
        invite = await create_invite_code(
            db,
            origin=InviteCodeOrigin.SYSTEM,
            max_uses=args.max_uses,
            expires_at=expires_at,
            note=args.note,
        )
        await db.commit()

    print(
        f"OK  code={invite.code}  "
        f"max_uses={invite.max_uses if invite.max_uses is not None else 'unlimited'}  "
        f"expires_at={expires_at.isoformat() if expires_at else 'never'}"
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--max-uses",
        type=int,
        default=None,
        help="Redemption cap. Omit for unlimited.",
    )
    parser.add_argument(
        "--expires-days",
        type=int,
        default=None,
        help="Days until the code expires. Omit for no expiry.",
    )
    parser.add_argument(
        "--note",
        default=None,
        help="Operator note stored on the row (e.g. 'beta wave 1').",
    )
    args = parser.parse_args()
    if args.max_uses is not None and args.max_uses < 1:
        parser.error("--max-uses must be >= 1")
    if args.expires_days is not None and args.expires_days < 1:
        parser.error("--expires-days must be >= 1")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
