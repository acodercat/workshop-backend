"""Recover payments that were paid at Huifu but never granted credits.

A lost async notification (e.g. order created before ``huifu_notify_url`` was
configured) combined with the reconciler expiring the order can leave an order
``EXPIRED`` / ``PENDING`` with no credits even though the customer paid. This
re-queries Huifu for each not-yet-granted order and, when Huifu confirms the
payment, grants the credits — idempotently (``apply_topup`` dedupes), and even
for ``EXPIRED`` orders — and accrues the referrer's commission, through the
same :func:`~app.services.payment.reconcile.settle_paid` the reconciler uses.

**Run this only with the current code** (``trans_stat`` mapped to
:class:`~app.services.payment.provider.ChargeState`, ``S`` → ``PAID``); an
older build under-detects payment and would skip real ones.

Usage::

    # Dry run — query Huifu and print each order's real status, grant nothing:
    uv run python -m scripts.recover_paid_orders --days 1 --dry-run

    # Actually grant credits for paid-but-ungranted orders in the last day:
    uv run python -m scripts.recover_paid_orders --days 1

    # A single known order:
    uv run python -m scripts.recover_paid_orders --out-trade-no hf<...>

``--dry-run`` also prints Huifu's raw query payload so you can confirm the
``trans_stat`` field before granting.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.payment_order import PaymentOrder, PaymentOrderStatus
from app.services.payment import orders, reconcile
from app.services.payment.provider import ChargeState


async def _candidate_ids(*, out_trade_no: str | None, days: int) -> list:
    """IDs of orders not yet granted (``ledger_id IS NULL``, not PAID)."""
    stmt = select(PaymentOrder.id).where(
        PaymentOrder.ledger_id.is_(None),
        PaymentOrder.status != PaymentOrderStatus.PAID,
    )
    if out_trade_no:
        stmt = stmt.where(PaymentOrder.out_trade_no == out_trade_no)
    else:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = stmt.where(PaymentOrder.created_at >= cutoff)
    async with AsyncSessionLocal() as db:
        rows = await db.execute(stmt.order_by(PaymentOrder.created_at.asc()))
        return [row[0] for row in rows.all()]


async def _process_one(order_id, *, dry_run: bool) -> str:
    async with AsyncSessionLocal() as db:
        order = await db.scalar(select(PaymentOrder).where(PaymentOrder.id == order_id))
        if order is None:
            return "not_found"
        if order.status == PaymentOrderStatus.PAID:
            return "already_paid"

        # Asked before the row is locked, as the reconciler does: a lock held
        # across the round trip would block this order's webhook.
        try:
            event = await orders.get_provider().query_charge(
                out_trade_no=order.out_trade_no,
                req_date=order.reference.get("req_date", ""),
            )
        except Exception as exc:  # noqa: BLE001 — ops tool, report and move on
            print(f"  {order.out_trade_no}: query_error: {exc}")
            return "query_error"

        paid = event.state is ChargeState.PAID
        print(
            f"  {order.out_trade_no} status={order.status.value} "
            f"provider_state={event.state} amount_fen={event.amount_fen}"
        )
        if dry_run:
            print(f"    raw={event.raw}")
            return "would_grant" if paid else "not_paid"
        if not paid:
            return "not_paid"

        order = await orders.lock_order(db, order.out_trade_no)
        if order is None or order.status == PaymentOrderStatus.PAID:
            return "already_paid"
        # Grants regardless of EXPIRED (only an already-PAID order is left
        # alone), checks the amount, is idempotent via apply_topup, and
        # accrues the referrer's commission like every other settle path.
        await reconcile.settle_paid(db, order, event)
        return "granted"


async def main(args: argparse.Namespace) -> int:
    ids = await _candidate_ids(out_trade_no=args.out_trade_no, days=args.days)
    print(f"Checking {len(ids)} not-yet-granted order(s) against Huifu...")
    summary: dict[str, int] = {}
    for order_id in ids:
        outcome = await _process_one(order_id, dry_run=args.dry_run)
        summary[outcome] = summary.get(outcome, 0) + 1
        print(f"order {order_id}: {outcome}")
    print("Summary:", summary)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out-trade-no", help="Recover a single order by its out_trade_no")
    parser.add_argument(
        "--days", type=int, default=1, help="Scan orders created in the last N days (default 1)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Query + print only; grant nothing")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
