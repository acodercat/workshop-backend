"""Manual balance correction — ``ADJUST`` ledger rows.

Different from :mod:`scripts.topup_credits` in two ways:

- ``--credits`` accepts **signed** integers (negative = clawback).
- ``--to`` is a debug convenience: pass a target balance and the
  script computes the delta against the current balance
  (:func:`~app.services.credits.apply_adjust_to` — settled and locked,
  and refused while a turn is in flight). Useful for resetting a dev
  account between test runs.

``--idempotency-key`` is required, as for ``topup_credits``: a retry of
the same correction must be a no-op, which only a key the operator chose
can promise. Same admin-gating posture — process auth is DB credentials.

Usage:
    # Explicit delta (production-style; pair with a stable key)
    uv run python -m scripts.adjust_credits \\
        --user-id=115 --credits=-500 \\
        --idempotency-key=incident-2026-05-15-refund \\
        --reason="incident #1234: double-charge correction"

    # Set to target (debug)
    uv run python -m scripts.adjust_credits \\
        --user-id=1 --to=0 --idempotency-key=dev-drain-2026-10-25 \\
        --reason="debug drain"
"""

import argparse
import asyncio
import sys

from app.database import AsyncSessionLocal
from app.services import credits
from app.services.credits import AdjustmentAlreadyApplied, BalanceNotSettled
from app.services.credits.rates import credits_from_micro, micro_from_credits


async def main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        try:
            if args.to is not None:
                entry = await credits.apply_adjust_to(
                    db,
                    user_id=args.user_id,
                    target_micro=micro_from_credits(args.to),
                    idempotency_key=args.idempotency_key,
                    reason=args.reason,
                )
            else:
                entry = await credits.apply_adjust(
                    db,
                    user_id=args.user_id,
                    amount_micro=micro_from_credits(args.credits),
                    idempotency_key=args.idempotency_key,
                    reason=args.reason,
                )
        except BalanceNotSettled as exc:
            print(f"Refused: {exc}; retry once it finishes.", file=sys.stderr)
            return 1
        except AdjustmentAlreadyApplied as exc:
            balance_micro = await credits.get_balance(db, args.user_id)
            print(
                f"OK (already applied)  user_id={args.user_id} "
                f"balance={credits_from_micro(balance_micro)} credits  ({exc})"
            )
            return 0

        await db.commit()
        if entry is None:
            print(f"OK (no-op)  user_id={args.user_id} balance already {args.to} credits")
            return 0
        balance_micro = await credits.get_balance(db, args.user_id)
        print(
            f"OK  user_id={args.user_id}  "
            f"ledger_id={entry.id}  "
            f"delta={credits_from_micro(entry.amount):+d} credits  "
            f"balance={credits_from_micro(balance_micro)} credits"
        )
        return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--user-id", type=int, required=True, help="Target user.id")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--credits",
        type=int,
        help="Signed delta in whole credits (negative = clawback). Non-zero.",
    )
    group.add_argument(
        "--to",
        type=int,
        metavar="N",
        help="Target balance in whole credits — script computes the delta.",
    )
    parser.add_argument(
        "--idempotency-key",
        required=True,
        help="Unique per logical correction; a retry with the same key is a no-op.",
    )
    parser.add_argument(
        "--reason",
        required=True,
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
