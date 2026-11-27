"""Admin top-up — add credits to one user's balance.

V1 admin-gating is "you have shell access to the box / DB credentials"
(see ``backend/CLAUDE.md`` for the rationale). An HTTP admin endpoint
arrives when there's a real internal review tool to gate.

Usage:
    # By RMB paid (domestic recharge — converts at settings.credit_price_cny, ¥0.1/credit):
    uv run python -m scripts.topup_credits --user-id=60 --cny=100 --idempotency-key=pay-2026-05-18-alice --reason="alipay order #1234"
    # Or by exact credit count:
    uv run python -m scripts.topup_credits --user-id=136 --credits=500000 --idempotency-key=manual-2026-10-04-135 --reason="赠送"

Give exactly one of ``--cny`` (RMB the customer paid, converted at the
``credit_price_cny`` sale price) or ``--credits`` (exact whole credits).
The script converts to µ-credits before persisting.

``--idempotency-key`` is required and must be unique per logical
operation. Re-running with the same key after a success exits 0 with
a "(already applied)" note — the previous call already moved the
balance. Different key → another top-up (possibly intentional).

On a fresh top-up the user is emailed a notice (amount added + new
balance), best-effort — a missing address or a send failure is logged
but never undoes the committed top-up, mirroring
``scripts/activate_and_notify.py``. The "(already applied)" path sends
nothing (the prior run already notified). Pass ``--no-email`` to skip
(e.g. silent backfills) and ``--locale`` to pick the language.
"""

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.user import User
from app.services import credits
from app.services.credits import TopupAlreadyApplied
from app.services.credits.rates import (
    credits_from_cny,
    credits_from_micro,
    micro_from_credits,
)
from app.services.email import (
    EmailSendError,
    render_credits_topped_up,
    send_html_email,
)
from config import settings


async def _notify_topup(
    db: AsyncSession,
    *,
    user_id: int,
    credits_added: int,
    balance: int,
    locale: str,
) -> None:
    """Email the user that their balance was topped up — best-effort.

    Mirrors ``scripts/activate_and_notify.py``: the top-up is the primary
    action and is already committed, so a missing address or a send
    failure is logged and swallowed, never rolled back. A user with no
    email on file is topped up just the same, only not notified.
    """
    row = (await db.execute(select(User.name, User.email).where(User.id == user_id))).first()
    if row is None:
        print(f"  [skip]   user_id={user_id}: 用户不存在，未发信")
        return
    name, email = row
    if not email:
        print(f"  [skip]   user_id={user_id} ({name}): 无邮箱，已充值但不发信")
        return
    subject, html_body = render_credits_topped_up(
        name=name,
        credits_added=credits_added,
        balance=balance,
        locale=locale,
    )
    try:
        rid = await send_html_email(to=email, subject=subject, html_body=html_body)
        print(f"  [sent]   user_id={user_id} → {email}  request_id={rid}")
    except EmailSendError as exc:
        print(
            f"  [FAILED] user_id={user_id} → {email}  {exc.message} "
            f"(code={exc.code} request_id={exc.request_id})"
        )


async def main(args: argparse.Namespace) -> int:
    if args.cny is not None:
        topup_credits = credits_from_cny(args.cny)
        source = f"¥{args.cny} @ ¥{settings.credit_price_cny}/credit"
    else:
        topup_credits = args.credits
        source = "direct"
    amount_micro = micro_from_credits(topup_credits)

    async with AsyncSessionLocal() as db:
        try:
            entry = await credits.apply_topup(
                db,
                user_id=args.user_id,
                amount_micro=amount_micro,
                idempotency_key=args.idempotency_key,
                reason=args.reason,
                source="admin",
            )
        except TopupAlreadyApplied as exc:
            # Re-fetch and show current balance so operators can verify
            # the prior call's effect.
            balance_micro = await credits.get_balance(db, args.user_id)
            print(
                f"OK (already applied)  user_id={args.user_id} "
                f"balance={credits_from_micro(balance_micro)} credits  ({exc})"
            )
            return 0

        await db.commit()
        balance_micro = await credits.get_balance(db, args.user_id)
        balance_credits = credits_from_micro(balance_micro)
        print(
            f"OK  user_id={args.user_id}  "
            f"ledger_id={entry.id}  "
            f"added={topup_credits} credits ({source})  "
            f"balance={balance_credits} credits"
        )
        if not args.no_email:
            print("\n发送充值通知:")
            await _notify_topup(
                db,
                user_id=args.user_id,
                credits_added=topup_credits,
                balance=balance_credits,
                locale=args.locale,
            )
        return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--user-id", type=int, required=True, help="Target user.id")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--credits",
        type=int,
        help="Exact whole credits to add. Must be positive.",
    )
    group.add_argument(
        "--cny",
        type=float,
        help="RMB the customer paid; converted at settings.credit_price_cny (¥0.1/credit).",
    )
    parser.add_argument(
        "--idempotency-key",
        required=True,
        help="Unique key; re-running with the same key is a safe no-op.",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Short human-readable reason (audited on the ledger row).",
    )
    parser.add_argument(
        "--locale",
        choices=["zh-CN", "en-US"],
        default="zh-CN",
        help="Notification email locale (default: zh-CN).",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Skip the top-up notification email (e.g. silent backfills).",
    )
    args = parser.parse_args()
    if args.credits is not None and args.credits <= 0:
        parser.error("--credits must be positive (use a future adjust script for clawbacks)")
    if args.cny is not None and args.cny <= 0:
        parser.error("--cny must be positive")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))


# uv run python -m scripts.topup_credits \
#     --user-id=<id> --credits=1000 \
#     --idempotency-key=manual-backfill-$(date +%Y%m%d) \
#     --reason="backfill: account pre-dates welcome grant"
