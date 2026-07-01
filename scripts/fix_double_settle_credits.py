"""One-shot balance correction for the double-settle race (fixed 2026-07-01).

Before the fix, ``settle_turn`` / ``refund_reservation`` could settle the same
reservation more than once under concurrency (a stale identity-map read
defeated the ``state is OPEN`` guard while ``FOR UPDATE`` held the lock — see
``_load_reservation_for_update``). That double-posted the settle rows for a set
of reservations, leaving each affected user's balance off by the net of the
duplicate ``+reserved / -actual`` pairs.

This reverses that error with **one idempotent ``ADJUST`` per affected user**:

    correction = -over_credit
    over_credit(reservation) = SUM(ledger for reservation) + actual_debit
                             (validated: a correctly-settled reservation -> 0)

Net-zero policy (operator-chosen): refunds users whose balance is too low AND
claws back users whose balance is too high. Idempotent on
``settle-dedup-fix:{user_id}`` so a re-run is a no-op. The duplicate settle rows
themselves are left on the ledger (append-only, audit trail); this only posts
the correcting entries.

Dry-run by default — pass ``--apply`` to write.

Usage:
    uv run python -m scripts.fix_double_settle_credits           # show the plan
    uv run python -m scripts.fix_double_settle_credits --apply   # write ADJUST rows
"""

import argparse
import asyncio
import sys

from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.services import credits
from app.services.credits import AdjustmentAlreadyApplied

_MICRO_PER_CREDIT = 10_000_000

# Per-user net correction (µ-credits). ``over_credit`` = the reservation's full
# ledger sum plus one actual debit (a correct single settle nets to -actual, so
# +actual == 0; any surplus is the duplicate-settle error). ``correction`` = its
# negation. Only reservations with >2 SETTLE rows were ever double-settled.
_PLAN_SQL = text(
    """
    WITH affected AS (
        SELECT reservation_id FROM credit_ledger WHERE kind = 'SETTLE'
        GROUP BY reservation_id HAVING count(*) > 2
    ),
    detail AS (
        SELECT cr.user_id,
               (SELECT COALESCE(SUM(amount), 0) FROM credit_ledger l
                  WHERE l.reservation_id = a.reservation_id)
             + (SELECT -amount FROM credit_ledger l
                  WHERE l.reservation_id = a.reservation_id
                    AND l.kind = 'SETTLE' AND l.amount < 0 LIMIT 1) AS over_micro
        FROM affected a JOIN credit_reservation cr ON cr.id = a.reservation_id
    )
    SELECT user_id, count(*) AS n_res, -SUM(over_micro) AS correction_micro
    FROM detail
    GROUP BY user_id
    HAVING -SUM(over_micro) <> 0
    ORDER BY -SUM(over_micro) DESC
    """
)

_IDEMPOTENCY_KEY = "settle-dedup-fix:{user_id}"
_REASON = (
    "double-settle race correction (fixed 2026-07-01): reverses the net balance "
    "error from {n_res} reservation(s) settled more than once"
)


def _fmt(micro: int) -> str:
    return f"{micro / _MICRO_PER_CREDIT:+,.2f}"


async def main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        plan = (await db.execute(_PLAN_SQL)).all()

    total = sum(int(r.correction_micro) for r in plan)
    print(f"affected users: {len(plan)}   net correction: {_fmt(total)} credits")
    for r in plan:
        print(f"  user {r.user_id:>5}: {r.n_res:>3} res   {_fmt(int(r.correction_micro)):>13} credits")

    if not args.apply:
        print("\nDRY-RUN — re-run with --apply to write the ADJUST rows.")
        return 0

    applied = skipped = 0
    async with AsyncSessionLocal() as db:
        for r in plan:
            key = _IDEMPOTENCY_KEY.format(user_id=r.user_id)
            try:
                entry = await credits.apply_adjust(
                    db,
                    user_id=r.user_id,
                    amount_micro=int(r.correction_micro),
                    idempotency_key=key,
                    reason=_REASON.format(n_res=r.n_res),
                )
                await db.commit()
                applied += 1
                print(f"  OK    user {r.user_id:>5}: {_fmt(int(r.correction_micro))} credits (ledger {entry.id})")
            except AdjustmentAlreadyApplied:
                skipped += 1
                print(f"  SKIP  user {r.user_id:>5}: already corrected ({key})")

    print(f"\ndone: applied={applied} skipped={skipped}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="Write the ADJUST rows. Omit for a dry-run.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
