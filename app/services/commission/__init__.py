"""Referral-commission wallet (cash side).

When an invitee recharges, the inviter earns a withdrawable **cash**
commission — a distinct liability from the prepaid credit balance, so it
lives in its own ledger (RMB cents) that mirrors
:mod:`app.services.credits`:

- :func:`accrue_for_order` posts a ``COMMISSION`` row when a paid order
  settles (best-effort, called by the payment webhook + reconciler after
  the buyer's top-up commits).
- :func:`request_withdrawal` debits the wallet and opens a cash-out
  request; :func:`mark_paid` / :func:`reject` are the operator
  settlement (``scripts/process_withdrawals.py``).
- :func:`convert_to_credits` turns commission into spendable credits.
- :func:`get_balance` + :func:`list_transactions` + :func:`list_withdrawals`
  are the read surface.

The double-entry invariant ``user.commission_balance_fen ==
SUM(commission_ledger.amount_fen)`` is preserved by funnelling every
write through :mod:`app.services.commission._internal`. Nothing else
should touch ``commission_ledger`` or ``user.commission_balance_fen``
directly.
"""

from app.services.commission.accrual import (
    CommissionAlreadyApplied,
    accrue_for_order,
    apply_commission,
)
from app.services.commission.balance import CommissionBalance, get_balance
from app.services.commission.convert import ConvertResult, convert_to_credits
from app.services.commission.transactions import (
    CommissionTransactionRow,
    list_transactions,
)
from app.services.commission.withdraw import (
    list_pending,
    list_withdrawals,
    mark_paid,
    reject,
    request_withdrawal,
)

__all__ = [
    "CommissionAlreadyApplied",
    "CommissionBalance",
    "CommissionTransactionRow",
    "ConvertResult",
    "accrue_for_order",
    "apply_commission",
    "convert_to_credits",
    "get_balance",
    "list_pending",
    "list_transactions",
    "list_withdrawals",
    "mark_paid",
    "reject",
    "request_withdrawal",
]
