"""user.status: add Postgres enum column

Adds a ``status`` column to ``user`` so login can reject non-``ACTIVE``
accounts (admin-disabled, risk-suspended, or freshly created and not
yet approved). Mirrors the project enum convention used by
``session.status`` (``session_status``) etc.: typed Postgres enum +
UPPERCASE labels matching the Python member names.

Forward shape:

1. ``CREATE TYPE user_status AS ENUM ('ACTIVE', 'DISABLED', 'SUSPENDED', 'PENDING')``
2. ``ALTER TABLE "user" ADD COLUMN status user_status NOT NULL
   DEFAULT 'PENDING'``
3. ``UPDATE "user" SET status = 'ACTIVE'`` — every existing row was
   able to log in before this migration; preserve that.

Revision ID: 14de0b363bda
Revises: c2e8f5a91b47
Create Date: 2026-05-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "14de0b363bda"
down_revision: Union[str, Sequence[str], None] = "c2e8f5a91b47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VALUES = ("ACTIVE", "DISABLED", "SUSPENDED", "PENDING")
_ENUM_NAME = "user_status"


def upgrade() -> None:
    op.execute(
        f"CREATE TYPE {_ENUM_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _VALUES)})"
    )

    op.add_column(
        "user",
        sa.Column(
            "status",
            sa.Enum(*_VALUES, name=_ENUM_NAME, create_type=False),
            nullable=False,
            server_default=sa.text(f"'PENDING'::{_ENUM_NAME}"),
        ),
    )

    # Backfill: every pre-migration user could already log in, so they
    # land on ``ACTIVE``. The server_default of ``PENDING`` only
    # applies to rows inserted *after* this migration.
    op.execute('UPDATE "user" SET status = \'ACTIVE\'')


def downgrade() -> None:
    op.drop_column("user", "status")
    op.execute(f"DROP TYPE IF EXISTS {_ENUM_NAME}")
