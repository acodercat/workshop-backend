"""add sandbox.expires_at

Records the deadline the OpenSandbox server is currently honouring for
this container — set when the worker first ``create``s it and bumped
on every per-turn ``renew``. The reaper reads this to decide whether a
sandbox is past its server-side TTL without having to round-trip to
the OpenSandbox API.

Nullable: existing rows have no recorded deadline (the worker will
backfill on their next renew). New rows get it on INSERT.

Revision ID: c7d2e8a4f1b6
Revises: b5e1c9a3d7f2
Create Date: 2026-05-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7d2e8a4f1b6"
down_revision: Union[str, Sequence[str], None] = "b5e1c9a3d7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``TIMESTAMP`` (no time zone) to match the existing ``created_at``
    # / ``last_used_at`` columns on this table.
    op.add_column(
        "sandbox",
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sandbox", "expires_at")
