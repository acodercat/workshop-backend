"""Revenue reporting — what counts as money received.

The one decision worth pinning: revenue is ``PAID`` orders and nothing
else. A ``PENDING`` order is a QR code someone may never scan, and
``EXPIRED`` / ``FAILED`` are intentions that ended; counting any of them
would report hope as cash.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import inspect

from app.models.payment_order import PaymentOrderStatus
from app.services.payment import analytics


def test_revenue_counts_paid_orders_only() -> None:
    assert analytics._PAID_ROWS == "status = 'PAID'"


def test_no_other_order_state_reaches_a_revenue_query() -> None:
    # The states that must never be summed as money.
    code = inspect.getsource(analytics).replace(analytics.__doc__ or "", "", 1)
    for status in (
        PaymentOrderStatus.PENDING,
        PaymentOrderStatus.EXPIRED,
        PaymentOrderStatus.FAILED,
    ):
        # PENDING appears once — the un-windowed stuck-order count, which
        # is a count of orders, never an amount.
        occurrences = code.count(f"'{status.value}'")
        if status is PaymentOrderStatus.PENDING:
            assert occurrences == 1
            assert "SUM(amount_fen)" not in _pending_query(code)
        else:
            assert occurrences == 0, f"{status.value} reaches a query"


def test_every_money_query_interpolates_the_shared_predicate() -> None:
    for query_owner in (analytics.summarize, analytics._daily_revenue):
        assert "{_PAID_ROWS}" in inspect.getsource(query_owner)

    code = inspect.getsource(analytics).replace(analytics.__doc__ or "", "", 1)
    assert code.count("status = 'PAID'") == 1


def test_pending_count_is_not_windowed() -> None:
    # A stuck order from last week is exactly the one worth surfacing, so
    # its query must carry no ``created_at`` bound.
    assert "created_at" not in _pending_query(
        inspect.getsource(analytics.summarize)
    )


def _pending_query(code: str) -> str:
    """The single-statement PENDING count, isolated from the module."""
    start = code.index("SELECT COUNT(*) FROM payment_order")
    return code[start : code.index('"', start)]
