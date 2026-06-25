"""Payment / recharge subsystem (buy-side).

Turns an external RMB payment into credits on the existing ledger:

- :func:`create_order` places a provider charge and persists a PENDING
  ``payment_order`` with the credit grant frozen at the current sale
  price.
- :func:`handle_webhook` applies a signature-verified async notification
  to its order (idempotent under at-least-once delivery).
- :func:`reconcile_order` is the pull-path backstop for a lost
  notification, driven by :mod:`app.workers.payment_reconciler`.
- :func:`get_order` is the poll read-surface; :func:`get_provider` exposes
  the gateway for the webhook to verify/ack against.

All provider/SDK specifics live behind the
:class:`~app.services.payment.provider.PaymentGateway` protocol in
``huifu.py``. Credits move only through ``credits.apply_topup``; this
package never writes ``credit_ledger`` / ``user.credit_balance``
directly.
"""

from app.services.payment.orders import (
    create_order,
    get_order,
    get_provider,
    handle_webhook,
    list_paid_orders,
)
from app.services.payment.reconcile import reconcile_order

__all__ = [
    "create_order",
    "get_order",
    "get_provider",
    "handle_webhook",
    "list_paid_orders",
    "reconcile_order",
]
