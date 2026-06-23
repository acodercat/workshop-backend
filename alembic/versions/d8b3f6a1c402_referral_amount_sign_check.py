"""extend ck_credit_ledger_amount_sign for REFERRAL

The amount-sign CHECK (added in ``f5d2c8a1e9b4``) predates the
``REFERRAL`` ledger kind, so it rejects every REFERRAL row. A referral
reward is always positive (credit-in to the referrer), so add
``(kind = 'REFERRAL' AND amount > 0)`` to the constraint.

Separate from the enum ``ADD VALUE`` migration (``c7a2f4e9b108``) on
purpose: Postgres won't let a newly-added enum value be *referenced*
in the same transaction it was added in, so the value must already be
committed before this constraint can name it.

Online-safe, mirroring ``f5d2c8a1e9b4``: ``ADD CONSTRAINT ... NOT
VALID`` takes only a brief lock; ``VALIDATE`` scans under SHARE UPDATE
EXCLUSIVE without blocking reads/writes. Existing rows already satisfy
the superset constraint.

Revision ID: d8b3f6a1c402
Revises: c7a2f4e9b108
Create Date: 2026-06-23

"""
from typing import Sequence, Union

from alembic import op


revision: str = "d8b3f6a1c402"
down_revision: Union[str, Sequence[str], None] = "c7a2f4e9b108"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEDGER_CK = "ck_credit_ledger_amount_sign"


def _set_check(*, with_referral: bool) -> None:
    op.execute(f"ALTER TABLE credit_ledger DROP CONSTRAINT IF EXISTS {_LEDGER_CK}")
    referral_clause = "(kind = 'REFERRAL' AND amount > 0)  OR\n" if with_referral else ""
    op.execute(
        f"""
        ALTER TABLE credit_ledger ADD CONSTRAINT {_LEDGER_CK} CHECK (
            (kind = 'TOPUP'   AND amount > 0)  OR
            (kind = 'RESERVE' AND amount < 0)  OR
            (kind = 'REFUND'  AND amount > 0)  OR
            (kind = 'ADJUST'  AND amount <> 0) OR
            {referral_clause}(kind = 'SETTLE')
        ) NOT VALID
        """
    )
    op.execute(f"ALTER TABLE credit_ledger VALIDATE CONSTRAINT {_LEDGER_CK}")


def upgrade() -> None:
    _set_check(with_referral=True)


def downgrade() -> None:
    _set_check(with_referral=False)
