"""Aggregate reporting over recharge orders.

Read-only, and in the ``payment`` package for the same reason
:mod:`app.services.credits.analytics` sits in ``credits``: the domain
that owns the table owns questions about it.

**Revenue is money actually received** — ``status = 'PAID'`` and nothing
else. A `PENDING` order is an intention (a QR code someone may never
scan), `EXPIRED` and `FAILED` are intentions that ended. Counting any of
them would report hope rather than cash.

Amounts stay in **RMB cents** (``amount_fen``) here, as the column does;
the endpoint converts to yuan for display. This is the one part of the
platform where money is genuinely RMB rather than the µ-credit ledger —
see the two-sided pricing note in ``CLAUDE.md``.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.business_time import business_days, business_tz, window_start

_PAID_ROWS = "status = 'PAID'"


@dataclass(frozen=True, slots=True)
class DailyRevenue:
    """One business day's takings. Quiet days are present with zero, so
    the chart's axis stays contiguous."""

    day: date
    revenue_fen: int


@dataclass(frozen=True, slots=True)
class RevenueSummary:
    """The payments page's tiles and chart, for one window.

    ``created_orders`` counts every order raised in the window whatever
    became of it — the denominator that makes ``paid_orders`` a
    conversion rate rather than a bare count.
    """

    revenue_fen: int
    paid_orders: int
    created_orders: int
    granted_micro: int
    pending_orders: int
    daily: list[DailyRevenue]


async def summarize(db: AsyncSession, *, days: int) -> RevenueSummary:
    """Aggregate recharge orders over the trailing ``days`` business days."""
    since = window_start(days)
    totals = (
        await db.execute(
            text(f"""
            SELECT
                COALESCE(SUM(amount_fen) FILTER (WHERE {_PAID_ROWS}), 0)
                    AS revenue_fen,
                COUNT(*) FILTER (WHERE {_PAID_ROWS})       AS paid_orders,
                COUNT(*)                                   AS created_orders,
                COALESCE(SUM(credit_micro) FILTER (WHERE {_PAID_ROWS}), 0)
                    AS granted_micro
            FROM payment_order
            WHERE created_at >= :since
            """),
            {"since": since},
        )
    ).one()

    # Orders still awaiting payment *right now*, regardless of window:
    # a stuck one from last week is exactly what an operator wants to
    # see, and windowing it away would hide the oldest cases first.
    pending = (
        await db.execute(text("SELECT COUNT(*) FROM payment_order WHERE status = 'PENDING'"))
    ).scalar_one()

    return RevenueSummary(
        revenue_fen=int(totals.revenue_fen),
        paid_orders=int(totals.paid_orders),
        created_orders=int(totals.created_orders),
        granted_micro=int(totals.granted_micro),
        pending_orders=int(pending),
        daily=await _daily_revenue(db, since=since, days=days),
    )


async def _daily_revenue(db: AsyncSession, *, since: datetime, days: int) -> list[DailyRevenue]:
    """Per-business-day takings, gap-filled to a contiguous series.

    Bucketed on ``created_at``: an order is paid within minutes of being
    raised (the QR expires in ``payment_order_ttl_seconds``), so this is
    also when the money arrived, and it keeps the series comparable with
    ``created_orders``.
    """
    rows = (
        await db.execute(
            text(f"""
            SELECT
                (created_at AT TIME ZONE :tz)::date AS day,
                SUM(amount_fen) AS revenue_fen
            FROM payment_order
            WHERE {_PAID_ROWS} AND created_at >= :since
            GROUP BY day
            """),
            {"since": since, "tz": business_tz().key},
        )
    ).all()

    revenue_by_day = {row.day: int(row.revenue_fen) for row in rows}
    return [
        DailyRevenue(day=day, revenue_fen=revenue_by_day.get(day, 0)) for day in business_days(days)
    ]
