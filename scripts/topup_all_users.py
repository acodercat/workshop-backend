"""One-off bulk top-up — grant every user in the ``user`` table credits.

Operational script (not part of the normal admin surface). Same
admin-gating posture as ``topup_credits`` — process auth is DB
credentials.

Per-user idempotency key ``bulk-topup-{TAG}-{user_id}`` so a re-run
(or resume after a partial failure) skips users already granted in
this batch instead of double-crediting. Each user gets its own
session/transaction so one bad row can't abort the rest.

Usage:
    uv run python -m scripts.topup_all_users --credits=200000--tag=20260516 --reason="bulk grant 200k"
"""

import argparse
import asyncio
import sys

from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.services import credits
from app.services.credits import TopupAlreadyApplied
from app.services.credits.rates import credits_from_micro, micro_from_credits


async def main(args: argparse.Namespace) -> int:
    amount_micro = micro_from_credits(args.credits)

    async with AsyncSessionLocal() as db:
        user_ids = list((await db.execute(text('SELECT id FROM "user" ORDER BY id'))).scalars())

    total = len(user_ids)
    print(f"bulk top-up: {total} users, +{args.credits} credits each, tag={args.tag}")

    granted = already = failed = 0
    for user_id in user_ids:
        async with AsyncSessionLocal() as db:
            try:
                await credits.apply_topup(
                    db,
                    user_id=user_id,
                    amount_micro=amount_micro,
                    idempotency_key=f"bulk-topup-{args.tag}-{user_id}",
                    reason=args.reason,
                )
                await db.commit()
                balance_micro = await credits.get_balance(db, user_id)
                granted += 1
                print(
                    f"  ok    user_id={user_id} balance={credits_from_micro(balance_micro)} credits"
                )
            except TopupAlreadyApplied:
                already += 1
                print(f"  skip  user_id={user_id} (already granted in tag={args.tag})")
            except Exception as exc:  # noqa: BLE001 — one bad row must not abort batch
                failed += 1
                print(f"  FAIL  user_id={user_id} ({exc!r})")

    print(f"done: granted={granted} already={already} failed={failed} total={total}")
    return 1 if failed else 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--credits",
        type=int,
        required=True,
        help="Whole credits to add to every user. Must be positive.",
    )
    parser.add_argument(
        "--tag",
        required=True,
        help="Batch tag baked into the per-user idempotency key. "
        "Reuse the same tag to safely resume; change it for a new round.",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Audited on every ledger row.",
    )
    args = parser.parse_args()
    if args.credits <= 0:
        parser.error("--credits must be positive")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
