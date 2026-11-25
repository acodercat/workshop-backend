"""project.pinned_at and sidebar_layout: pinned projects, and the order a
user arranges their sidebar in

``project.pinned_at`` is nullable with no default, as ``session.pinned_at``
is: NULL is "not pinned", so no row of ``project`` is rewritten.
``sidebar_layout`` holds one row per user, created by their first save.

Revision ID: 9bd99073dc16
Revises: c2f7a9e4b518
Create Date: 2026-11-19

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9bd99073dc16"
down_revision: Union[str, Sequence[str], None] = "c2f7a9e4b518"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("project", sa.Column("pinned_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_table(
        "sidebar_layout",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "pin_order",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "project_order",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("sidebar_layout")
    op.drop_column("project", "pinned_at")
