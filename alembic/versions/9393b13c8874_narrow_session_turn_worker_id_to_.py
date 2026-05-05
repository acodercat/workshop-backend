"""narrow session_turn.worker_id to varchar(255)

The column was originally ``TEXT`` (unbounded) but its values are
short identifiers — the worker writes ``hostname-pid`` by default
(at most ~72 chars), or an operator-supplied ``--worker-name`` label.
Every other identity column in the schema is bounded ``VARCHAR(N)``
(``user.name`` 255, ``session.title`` 255, ``session.model_id`` 64,
``user.phone`` 20); ``worker_id`` was the lone unbounded outlier.

Postgres stores ``TEXT`` and ``VARCHAR(N)`` identically on disk —
this migration is purely a length-validation contract change. The
``ALTER COLUMN ... TYPE`` triggers a one-time table scan to verify
existing values fit; all current production identities are well
within 255 chars, so the scan only locks the table briefly.

Revision ID: 9393b13c8874
Revises: b8d2f4a6c7e9
Create Date: 2026-05-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9393b13c8874"
down_revision: Union[str, Sequence[str], None] = "b8d2f4a6c7e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "session_turn",
        "worker_id",
        existing_type=sa.TEXT(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "session_turn",
        "worker_id",
        existing_type=sa.String(length=255),
        type_=sa.TEXT(),
        existing_nullable=True,
    )
