"""commission_ledger CHECK constraints (sign + settlement-hold coherence)

DB-level backstop for two invariants previously enforced **only** in
Python (``app/services/commission/_internal.py`` single-writer),
mirroring what ``f5d2c8a1e9b4`` did for the credit ledger. This wallet
is real, withdrawable cash — a corrupt row here exits the system as RMB
via ``scripts/process_withdrawals.py`` — so it deserves at least the
guardrails the (softer) credit ledger already has.

1. ``commission_ledger`` — ``amount_fen`` sign must match ``kind``
   (conventions from the model docstring, verified against every
   writer in ``app/services/commission/``):

   - ``COMMISSION``      amount_fen > 0  (``apply_commission`` rejects <= 0)
   - ``WITHDRAW``        amount_fen < 0  (``request_withdrawal``: ``-amount``)
   - ``WITHDRAW_REFUND`` amount_fen > 0  (reject path: ``+request.amount_fen``)
   - ``CONVERT``         amount_fen < 0  (``convert_to_credits``: ``-spent``)
   - ``CLAWBACK``        amount_fen < 0  (no writer yet; docstring contract)
   - ``ADJUST``          amount_fen <> 0 (signed operator correction)

2. ``commission_ledger`` — ``available_at`` (the settlement hold) is
   carried by ``COMMISSION`` rows and only by them:

   - ``COMMISSION``   available_at IS NOT NULL (accrual always stamps
     ``created_at + commission_hold_days``)
   - every other kind available_at IS NULL (debits / reversals are
     effective at once)

Online-safe, same idiom as ``f5d2c8a1e9b4``: ``ADD CONSTRAINT ... NOT
VALID`` takes only a brief lock to record the constraint; ``VALIDATE
CONSTRAINT`` scans under SHARE UPDATE EXCLUSIVE without blocking
concurrent reads/writes. Existing rows are expected to pass — they were
all written through the single-writer funnel.

Revision ID: b2d6f1a8c4e3
Revises: d4a8f1c6e2b9
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op


revision: str = "b2d6f1a8c4e3"
down_revision: Union[str, Sequence[str], None] = "d4a8f1c6e2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SIGN_CK = "ck_commission_ledger_amount_sign"
_HOLD_CK = "ck_commission_ledger_kind_available_at"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE commission_ledger ADD CONSTRAINT {_SIGN_CK} CHECK (
            (kind = 'COMMISSION'      AND amount_fen > 0)  OR
            (kind = 'WITHDRAW'        AND amount_fen < 0)  OR
            (kind = 'WITHDRAW_REFUND' AND amount_fen > 0)  OR
            (kind = 'CONVERT'         AND amount_fen < 0)  OR
            (kind = 'CLAWBACK'        AND amount_fen < 0)  OR
            (kind = 'ADJUST'          AND amount_fen <> 0)
        ) NOT VALID
        """
    )
    op.execute(f"ALTER TABLE commission_ledger VALIDATE CONSTRAINT {_SIGN_CK}")

    op.execute(
        f"""
        ALTER TABLE commission_ledger ADD CONSTRAINT {_HOLD_CK} CHECK (
            (kind = 'COMMISSION') = (available_at IS NOT NULL)
        ) NOT VALID
        """
    )
    op.execute(f"ALTER TABLE commission_ledger VALIDATE CONSTRAINT {_HOLD_CK}")


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE commission_ledger DROP CONSTRAINT IF EXISTS {_HOLD_CK}"
    )
    op.execute(
        f"ALTER TABLE commission_ledger DROP CONSTRAINT IF EXISTS {_SIGN_CK}"
    )
