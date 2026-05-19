"""drop sandbox.expires_at

The column held the OpenSandbox server-side TTL deadline back when
containers auto-expired and the reaper reconciled against it.
Sandboxes are now created with ``timeout=None`` (no server TTL) and
the reaper reclaims purely from DB facts — ``last_used_at`` idle age
or a soft-deleted session — so nothing writes or reads ``expires_at``
anymore. Drop the dead column.

- ``DROP COLUMN`` of a nullable column is an instant catalog-only
  change (no table rewrite); only a brief ``ACCESS EXCLUSIVE`` lock,
  so no ``CONCURRENTLY`` dance is needed here.
- ``downgrade`` re-adds the column exactly as ``c7d2e8a4f1b6`` first
  created it (nullable, no time zone, matching ``created_at`` /
  ``last_used_at``); the historical deadline values are gone for
  good, which is fine — no code consumes the column any longer.

Revision ID: d3a7f1c9b6e2
Revises: a7e3f1b9d6c4
Create Date: 2026-05-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d3a7f1c9b6e2"
down_revision: Union[str, Sequence[str], None] = "a7e3f1b9d6c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("sandbox", "expires_at")


def downgrade() -> None:
    # ``TIMESTAMP`` (no time zone) to match the existing ``created_at``
    # / ``last_used_at`` columns on this table.
    op.add_column(
        "sandbox",
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=True),
    )
