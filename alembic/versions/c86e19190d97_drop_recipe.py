"""drop recipe

Recipes shipped on the backend only: no frontend offered a picker, and the
worker never read ``session_turn.recipe``, so a ``recipe_id`` sent with
a message was validated, snapshotted and then silently ignored. Removed with
its code rather than kept as a feature that answers yes and does nothing.

**Data.** Drops the ``recipe`` table, the ``recipe_origin`` type and the
``session_turn.recipe`` column. The downgrade recreates all three empty.

Revision ID: c86e19190d97
Revises: ecaa1f0bc3f6
Create Date: 2026-10-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c86e19190d97"
down_revision: Union[str, Sequence[str], None] = "ecaa1f0bc3f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ORIGIN = "recipe_origin"


def upgrade() -> None:
    op.drop_column("session_turn", "recipe")
    op.drop_table("recipe")
    op.execute(f"DROP TYPE {_ORIGIN}")


def downgrade() -> None:
    op.execute(f"CREATE TYPE {_ORIGIN} AS ENUM ('OFFICIAL', 'CUSTOM')")
    op.create_table(
        "recipe",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "origin",
            postgresql.ENUM("OFFICIAL", "CUSTOM", name=_ORIGIN, create_type=False),
            nullable=False,
            server_default=sa.text("'CUSTOM'"),
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "name", name="uq_recipe_user_id_name"),
        sa.CheckConstraint(
            "(origin = 'OFFICIAL' AND user_id IS NULL) "
            "OR (origin = 'CUSTOM' AND user_id IS NOT NULL)",
            name="ck_recipe_origin_user_id_consistent",
        ),
    )
    op.create_index(
        "uq_recipe_platform_name",
        "recipe",
        ["name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.add_column("session_turn", sa.Column("recipe", postgresql.JSONB(), nullable=True))
