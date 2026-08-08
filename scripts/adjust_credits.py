"""Manual balance correction — ``ADJUST`` ledger rows.

Different from :mod:`scripts.topup_credits` in two ways:

- ``--credits`` accepts **signed** integers (negative = clawback).
- ``--to`` is a debug convenience: pass a target balance and the
  script computes the delta against the current balance. Useful
  for resetting a dev account between test runs.

Same admin-gating posture as ``topup_credits`` — process auth is
DB credentials.

Usage:
    # Explicit delta (production-style; pair with a stable key)
    uv run python -m scripts.adjust_credits \\
        --user-id=115 --credits=-500 \\
        --idempotency-key=incident-2026-05-15-refund \\
        --reason="incident #1234: double-charge correction"

    # Set to target (debug; auto-generated key)
    uv run python -m scripts.adjust_credits \\
        --user-id=1 --to=0 --reason="debug drain"
"""

import argparse
import asyncio
import sys
import time

from app.database import AsyncSessionLocal
from app.services import credits
from app.services.credits import AdjustmentAlreadyApplied
from app.services.credits.rates import credits_from_micro, micro_from_credits


async def main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        if args.to is not None:
            current_micro = await credits.get_balance(db, args.user_id)
            delta_micro = micro_from_credits(args.to) - current_micro
            if delta_micro == 0:
                print(
                    f"OK (no-op)  user_id={args.user_id} "
                    f"balance already {args.to} credits"
                )
                return 0
        else:
            delta_micro = micro_from_credits(args.credits)

        idempotency_key = (
            args.idempotency_key
            or f"manual-{args.user_id}-{int(time.time())}"
        )

        try:
            entry = await credits.apply_adjust(
                db,
                user_id=args.user_id,
                amount_micro=delta_micro,
                idempotency_key=idempotency_key,
                reason=args.reason,
            )
        except AdjustmentAlreadyApplied as exc:
            balance_micro = await credits.get_balance(db, args.user_id)
            print(
                f"OK (already applied)  user_id={args.user_id} "
                f"balance={credits_from_micro(balance_micro)} credits  ({exc})"
            )
            return 0

        await db.commit()
        balance_micro = await credits.get_balance(db, args.user_id)
        print(
            f"OK  user_id={args.user_id}  "
            f"ledger_id={entry.id}  "
            f"delta={credits_from_micro(delta_micro):+d} credits  "
            f"balance={credits_from_micro(balance_micro)} credits"
        )
        return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--user-id", type=int, required=True, help="Target user.id")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--credits", type=int,
        help="Signed delta in whole credits (negative = clawback). Non-zero.",
    )
    group.add_argument(
        "--to", type=int, metavar="N",
        help="Target balance in whole credits — script computes the delta.",
    )
    parser.add_argument(
        "--idempotency-key",
        help="Optional. Auto-generated from (user_id, epoch) when omitted.",
    )
    parser.add_argument(
        "--reason", required=True,
        help="Short human-readable reason (audited on the ledger row).",
    )
    args = parser.parse_args()
    if args.credits is not None and args.credits == 0:
        parser.error("--credits must be non-zero (no-op rows add audit noise)")
    if args.to is not None and args.to < 0:
        parser.error("--to target must be >= 0")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
