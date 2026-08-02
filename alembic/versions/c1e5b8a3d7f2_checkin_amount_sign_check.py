"""extend ck_credit_ledger_amount_sign for CHECKIN

The amount-sign CHECK (``f5d2c8a1e9b4``, extended for REFERRAL in
``d8b3f6a1c402``) predates the ``CHECKIN`` ledger kind, so it rejects
every CHECKIN row. A daily check-in reward is always positive
(credit-in to the user), so add ``(kind = 'CHECKIN' AND amount > 0)``.

Separate from the enum ``ADD VALUE`` migration (``a3f7c2d9e1b4``) on
purpose: Postgres won't let a newly-added enum value be *referenced* in
the same transaction it was added in, so the value must already be
committed before this constraint can name it.

Online-safe, mirroring ``f5d2c8a1e9b4`` / ``d8b3f6a1c402``: ``ADD
CONSTRAINT ... NOT VALID`` takes only a brief lock; ``VALIDATE`` scans
under SHARE UPDATE EXCLUSIVE without blocking reads/writes. Existing
rows already satisfy the superset constraint.

Revision ID: c1e5b8a3d7f2
Revises: b8d1e4a7c3f6
Create Date: 2026-07-01

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c1e5b8a3d7f2"
down_revision: Union[str, Sequence[str], None] = "b8d1e4a7c3f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEDGER_CK = "ck_credit_ledger_amount_sign"


def _set_check(*, with_checkin: bool) -> None:
    op.execute(f"ALTER TABLE credit_ledger DROP CONSTRAINT IF EXISTS {_LEDGER_CK}")
    checkin_clause = "(kind = 'CHECKIN' AND amount > 0)  OR\n" if with_checkin else ""
    op.execute(
        f"""
        ALTER TABLE credit_ledger ADD CONSTRAINT {_LEDGER_CK} CHECK (
            (kind = 'TOPUP'    AND amount > 0)  OR
            (kind = 'RESERVE'  AND amount < 0)  OR
            (kind = 'REFUND'   AND amount > 0)  OR
            (kind = 'ADJUST'   AND amount <> 0) OR
            (kind = 'REFERRAL' AND amount > 0)  OR
            {checkin_clause}(kind = 'SETTLE')
        ) NOT VALID
        """
    )
    op.execute(f"ALTER TABLE credit_ledger VALIDATE CONSTRAINT {_LEDGER_CK}")


def upgrade() -> None:
    _set_check(with_checkin=True)


def downgrade() -> None:
    _set_check(with_checkin=False)
