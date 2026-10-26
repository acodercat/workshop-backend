"""Operator tool for settling commission withdrawals (v1: manual payout).

Gated by shell access to the box / DB credentials (see ``backend/CLAUDE.md``),
same as ``scripts/topup_credits.py``. The admin console has no withdrawal
surface yet; this is the payout path.

Usage:
    # List PENDING withdrawals to review:
    uv run python -m scripts.process_withdrawals --list

    # Pay one out after transferring the money, recording the transfer ref:
    uv run python -m scripts.process_withdrawals --pay <id> --ref "alipay-20260624-001"

    # Reject one (returns the funds to the user's wallet):
    uv run python -m scripts.process_withdrawals --reject <id> --reason "账户信息有误"

The funds were already debited when the user submitted the request, so
``--pay`` only flips the status + records the reference; ``--reject``
posts a refund row that returns the funds. Both are idempotent at the
status guard (a non-PENDING request errors out rather than double-acting).
"""

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.user import User
from app.services import commission
from app.services.commission.rates import cny_from_fen

_LIST_LIMIT = 200


async def _list(db: AsyncSession) -> int:
    pending = await commission.list_pending(db, limit=_LIST_LIMIT)
    if not pending:
        print("No PENDING withdrawals.")
        return 0
    print(f"{len(pending)} PENDING withdrawal(s):\n")
    for req in pending:
        name = await db.scalar(select(User.name).where(User.id == req.user_id))
        print(
            f"  {req.id}\n"
            f"    user_id={req.user_id} ({name})  amount=¥{cny_from_fen(req.amount_fen):.2f}\n"
            f"    alipay={req.payout_account}  name={req.payout_name}\n"
            f"    created={req.created_at.isoformat()}\n"
        )
    return 0


async def _pay(db: AsyncSession, *, withdrawal_id: uuid.UUID, ref: str) -> int:
    request = await commission.mark_paid(db, withdrawal_id=withdrawal_id, payout_ref=ref)
    await db.commit()
    print(
        f"OK paid  id={request.id}  amount=¥{cny_from_fen(request.amount_fen):.2f}  "
        f"payout_ref={ref}"
    )
    return 0


async def _reject(db: AsyncSession, *, withdrawal_id: uuid.UUID, reason: str) -> int:
    request = await commission.reject(db, withdrawal_id=withdrawal_id, reason=reason)
    await db.commit()
    print(
        f"OK rejected  id={request.id}  amount=¥{cny_from_fen(request.amount_fen):.2f} "
        f"returned to wallet  reason={reason!r}"
    )
    return 0


async def main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        if args.list:
            return await _list(db)
        if args.pay is not None:
            return await _pay(db, withdrawal_id=args.pay, ref=args.ref)
        return await _reject(db, withdrawal_id=args.reject, reason=args.reason)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--list",
        action="store_true",
        help="List PENDING withdrawals.",
    )
    action.add_argument(
        "--pay",
        type=uuid.UUID,
        metavar="ID",
        help="Mark a withdrawal PAID.",
    )
    action.add_argument(
        "--reject",
        type=uuid.UUID,
        metavar="ID",
        help="Reject a withdrawal.",
    )
    parser.add_argument(
        "--ref",
        help="Payout transfer reference (required with --pay).",
    )
    parser.add_argument(
        "--reason",
        help="Rejection reason (required with --reject).",
    )
    args = parser.parse_args()
    if args.pay is not None and not args.ref:
        parser.error("--pay requires --ref")
    if args.reject is not None and not args.reason:
        parser.error("--reject requires --reason")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
