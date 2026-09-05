"""drop admin_audit_log.reason

The console asked every sensitive mutation for a typed justification —
a ticket id, an incident number — and stored it here. It no longer asks
for one anywhere, so the column has no writer left.

What the trail still answers is unchanged: ``staff_id`` says who,
``created_at`` when, ``action`` + ``target_type``/``target_id`` what was
acted on, and ``payload`` carries the parameters and the before → after
diff. Only the operator's prose is gone.

**The text is not recoverable.** ``downgrade`` restores the column,
nullable and empty; there is nothing to back-fill it from. Take a copy
first if the history matters::

    CREATE TABLE admin_audit_log_reason_archive AS
    SELECT id, reason FROM admin_audit_log WHERE reason IS NOT NULL;

Deploy the application code **before** running this: the ORM stops
mapping the column in the same change, and a model that still maps a
dropped column breaks every read and write of the table.

Revision ID: b6d2f8a04c17
Revises: a7c3f1d9e4b2
Create Date: 2026-09-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b6d2f8a04c17"
down_revision: Union[str, Sequence[str], None] = "a7c3f1d9e4b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("admin_audit_log", "reason")


def downgrade() -> None:
    # Nullable and empty — the column comes back, the text does not.
    op.add_column(
        "admin_audit_log",
        sa.Column("reason", sa.Text(), nullable=True),
    )
