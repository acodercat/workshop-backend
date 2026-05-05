"""drop session_run.error

Field was operator-facing only — written by the worker on every
``fail_run`` and ``expire_stale_runs`` but never read by any code
(backend, worker, or frontend). Worker logs already carry the
authoritative failure detail with stack traces; the column was a
short stable string at best ("runner failed" / "lease expired") and
duplicated information already present elsewhere.

Revision ID: e3a8b6f4c1d2
Revises: c7d2e8a4f1b6
Create Date: 2026-05-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3a8b6f4c1d2"
down_revision: Union[str, Sequence[str], None] = "c7d2e8a4f1b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("session_run", "error")


def downgrade() -> None:
    op.add_column(
        "session_run",
        sa.Column("error", sa.Text(), nullable=True),
    )
