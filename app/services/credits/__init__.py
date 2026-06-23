"""Credit / quota subsystem.

PAYG credit ledger sitting in front of the LLM turn pipeline:

- :func:`reserve_for_turn` debits a max-estimate before the agent
  starts; :func:`settle_turn` posts the actual cost using token
  counts that the worker persisted to ``session_turn`` in the same
  fenced transaction as its terminal status flip;
  :func:`refund_reservation` zeros the hold on crash / cancel.
- :func:`apply_topup` is the admin-only path to add credit balance.
- :func:`apply_adjust` is the signed correction primitive.
- :func:`get_balance` + :func:`list_transactions` are the read surface.
- :func:`settle_open_for_user` is the lazy sweep called from
  user-facing endpoints (``GET /credits/balance``,
  ``GET /credits/transactions``, ``queue_user_message``).
  :func:`settle_open_for_session` is the per-session variant used by
  the background sweeper (:mod:`app.workers.settle_sweeper`) and the
  pub/sub subscriber (:mod:`app.workers.settle_subscriber`).

The double-entry invariant
``user.credit_balance == SUM(credit_ledger.amount)`` is preserved by
funnelling every write through :mod:`app.services.credits._internal`.
Nothing else in the codebase should touch ``credit_ledger`` or
``user.credit_balance`` directly.

See ``docs/credits-future.md`` for the deferred work (sandbox-seconds
billing, worker-side cost cap, payment-driven top-ups, multi-org
billing).
"""

from app.services.credits.adjust import AdjustmentAlreadyApplied, apply_adjust
from app.services.credits.balance import get_balance
from app.services.credits.referral import (
    ReferralRewardAlreadyApplied,
    apply_referral_reward,
)
from app.services.credits.reserve import reserve_for_turn
from app.services.credits.settle import (
    refund_reservation,
    settle_open_for_session,
    settle_open_for_user,
    settle_turn,
)
from app.services.credits.topup import TopupAlreadyApplied, apply_topup
from app.services.credits.transactions import (
    TransactionKind,
    TransactionRow,
    list_transactions,
)

__all__ = [
    "AdjustmentAlreadyApplied",
    "ReferralRewardAlreadyApplied",
    "TopupAlreadyApplied",
    "TransactionKind",
    "TransactionRow",
    "apply_adjust",
    "apply_referral_reward",
    "apply_topup",
    "get_balance",
    "list_transactions",
    "refund_reservation",
    "reserve_for_turn",
    "settle_open_for_session",
    "settle_open_for_user",
    "settle_turn",
]
