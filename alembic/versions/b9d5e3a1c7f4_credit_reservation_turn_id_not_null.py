"""credit_reservation: turn_id SET NOT NULL

After dropping ``kind`` / ``sandbox_id`` (c4d7a2b9e1f6) every reservation
is a TURN reservation, and ``reserve_for_turn`` (the only writer) always
populates ``turn_id`` — leaving it nullable invited drift between intent
and schema. ``SET NOT NULL`` makes the invariant a DB-enforced contract;
the historical settle path's defensive ``turn_id IS NULL`` branch
becomes dead code and is removed in the same change.

Pre-check: ``SELECT count(*) FROM credit_reservation WHERE turn_id IS
NULL`` was 0 at the time of writing; ``SET NOT NULL`` would fail on the
first row otherwise. If you're applying this on a different database,
verify the same before running.

Postgres briefly takes ``ACCESS EXCLUSIVE`` to revalidate the column;
on the typically-small ``credit_reservation`` table this is a sub-second
lock with no rewrite.

Revision ID: b9d5e3a1c7f4
Revises: c4d7a2b9e1f6
Create Date: 2026-05-20
"""
from typing import Sequence, Union

from alembic import op


revision: str = "b9d5e3a1c7f4"
down_revision: Union[str, Sequence[str], None] = "c4d7a2b9e1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("credit_reservation", "turn_id", nullable=False)


def downgrade() -> None:
    op.alter_column("credit_reservation", "turn_id", nullable=True)
