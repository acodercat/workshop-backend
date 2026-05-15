"""Admin top-up — add credits to one user's balance.

V1 admin-gating is "you have shell access to the box / DB credentials"
(see ``backend/CLAUDE.md`` for the rationale). An HTTP admin endpoint
arrives when there's a real internal review tool to gate.

Usage:
    uv run python -m scripts.topup_credits \\
        --user-id=42 \\
        --credits=10000 \\
        --idempotency-key=manual-2026-05-14-alice \\
        --reason="trial grant for internal testing"

``--credits`` is whole credits (1 credit = $0.001 USD of LLM cost).
The script converts to µ-credits before persisting.

``--idempotency-key`` is required and must be unique per logical
operation. Re-running with the same key after a success exits 0 with
a "(already applied)" note — the previous call already moved the
balance. Different key → another top-up (possibly intentional).
"""

import argparse
import asyncio
import sys

from app.database import AsyncSessionLocal
from app.services import credits
from app.services.credits import TopupAlreadyApplied
from app.services.credits.rates import micro_from_credits


async def main(args: argparse.Namespace) -> int:
    amount_micro = micro_from_credits(args.credits)

    async with AsyncSessionLocal() as db:
        try:
            entry = await credits.apply_topup(
                db,
                user_id=args.user_id,
                amount_micro=amount_micro,
                idempotency_key=args.idempotency_key,
                reason=args.reason,
            )
        except TopupAlreadyApplied as exc:
            # Re-fetch and show current balance so operators can verify
            # the prior call's effect.
            balance_micro = await credits.get_balance(db, args.user_id)
            print(
                f"OK (already applied)  user_id={args.user_id} "
                f"balance={balance_micro // 1_000_000} credits  ({exc})"
            )
            return 0

        await db.commit()
        balance_micro = await credits.get_balance(db, args.user_id)
        print(
            f"OK  user_id={args.user_id}  "
            f"ledger_id={entry.id}  "
            f"added={args.credits} credits  "
            f"balance={balance_micro // 1_000_000} credits"
        )
        return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--user-id", type=int, required=True, help="Target user.id")
    parser.add_argument(
        "--credits", type=int, required=True,
        help="Whole credits to add (1 credit ≈ $0.001 USD). Must be positive.",
    )
    parser.add_argument(
        "--idempotency-key", required=True,
        help="Unique key; re-running with the same key is a safe no-op.",
    )
    parser.add_argument(
        "--reason", required=True,
        help="Short human-readable reason (audited on the ledger row).",
    )
    args = parser.parse_args()
    if args.credits <= 0:
        parser.error("--credits must be positive (use a future adjust script for clawbacks)")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
