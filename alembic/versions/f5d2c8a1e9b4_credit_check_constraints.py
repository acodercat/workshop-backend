"""credit_ledger + credit_reservation CHECK constraints

DB-level backstop for two invariants previously enforced **only** in
Python (``app/services/credits/_internal.py`` single-writer). A stray
manual ``psql`` UPDATE / a buggy import script could otherwise leave
the ledger permanently inconsistent with no guardrail.

1. ``credit_ledger`` — ``amount`` sign must match ``kind``:

   - ``TOPUP``   amount > 0   (``apply_topup`` rejects <= 0)
   - ``RESERVE`` amount < 0   (``reserve``: ``-reserved``, reserved >= 1)
   - ``REFUND``  amount > 0   (``refund_reservation``: ``+reserved``)
   - ``ADJUST``  amount <> 0  (``apply_adjust`` rejects 0)
   - ``SETTLE``  **unconstrained** — by design two paired rows per
     settle: ``+reserved`` (reversal) and ``-actual`` (debit), and
     ``actual`` can be 0 for a zero-token turn. No sign rule applies.

2. ``credit_reservation`` — ``settled_at`` coherence with ``state``:

   - ``OPEN``                 settled_at IS NULL
   - ``SETTLED`` / ``CANCELLED`` settled_at IS NOT NULL
     (both ``settle_turn`` and ``refund_reservation`` stamp
     ``settled_at = now()`` on the terminal transition).

Online-safe: ``ADD CONSTRAINT ... NOT VALID`` only takes a brief lock
to record the constraint (no table scan); the subsequent
``VALIDATE CONSTRAINT`` scans under ``SHARE UPDATE EXCLUSIVE`` which
does not block concurrent reads/writes. Existing rows are expected to
pass — they were all written through the single-writer path.

Revision ID: f5d2c8a1e9b4
Revises: e4b9c1a7f3d2
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f5d2c8a1e9b4"
down_revision: Union[str, Sequence[str], None] = "e4b9c1a7f3d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEDGER_CK = "ck_credit_ledger_amount_sign"
_RESERVATION_CK = "ck_credit_reservation_state_settled_at"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE credit_ledger ADD CONSTRAINT {_LEDGER_CK} CHECK (
            (kind = 'TOPUP'   AND amount > 0)  OR
            (kind = 'RESERVE' AND amount < 0)  OR
            (kind = 'REFUND'  AND amount > 0)  OR
            (kind = 'ADJUST'  AND amount <> 0) OR
            (kind = 'SETTLE')
        ) NOT VALID
        """
    )
    op.execute(f"ALTER TABLE credit_ledger VALIDATE CONSTRAINT {_LEDGER_CK}")

    op.execute(
        f"""
        ALTER TABLE credit_reservation ADD CONSTRAINT {_RESERVATION_CK} CHECK (
            (state = 'OPEN' AND settled_at IS NULL) OR
            (state IN ('SETTLED', 'CANCELLED') AND settled_at IS NOT NULL)
        ) NOT VALID
        """
    )
    op.execute(
        f"ALTER TABLE credit_reservation VALIDATE CONSTRAINT {_RESERVATION_CK}"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE credit_reservation DROP CONSTRAINT IF EXISTS {_RESERVATION_CK}"
    )
    op.execute(
        f"ALTER TABLE credit_ledger DROP CONSTRAINT IF EXISTS {_LEDGER_CK}"
    )
