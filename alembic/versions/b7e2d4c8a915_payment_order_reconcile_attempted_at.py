"""payment_order.reconcile_attempted_at: round-robin the reconciler's PENDING set

The payment reconciler took its batch oldest-expiry first. Now that an
order the provider has given no verdict on (still "processing", or no
answer at all) stays PENDING past its TTL for up to a day instead of being
expired on the clock, a full batch of those would stand in front of every
newer PENDING order — including one that was paid and whose notification
was lost — until they aged out. So every attempt is stamped on the row and
the batch is taken least-recently-attempted first (NULL, never tried,
first of all); the ``status = 'PENDING'`` partial index moves from
``expires_at`` to the stamp to keep backing that scan.

Plain (not ``CONCURRENTLY``) DDL, unlike ``f4c8a2b91d63`` just before it:
that one indexed ``session``, which is written on every turn, so a build
holding a ``SHARE`` lock was the whole problem. ``payment_order`` is
small and written only when an order is created or settles, so the brief
``ACCESS EXCLUSIVE`` of a swap costs nothing worth an ``autocommit_block``.

Revision ID: b7e2d4c8a915
Revises: a3f9c1d7e2b4
Create Date: 2026-09-07

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e2d4c8a915"
down_revision: Union[str, Sequence[str], None] = "a3f9c1d7e2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payment_order",
        sa.Column("reconcile_attempted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.drop_index("ix_payment_order_pending", table_name="payment_order")
    op.create_index(
        "ix_payment_order_pending",
        "payment_order",
        [sa.text("reconcile_attempted_at NULLS FIRST")],
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index("ix_payment_order_pending", table_name="payment_order")
    op.create_index(
        "ix_payment_order_pending",
        "payment_order",
        ["expires_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.drop_column("payment_order", "reconcile_attempted_at")
