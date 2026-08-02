"""create recipe + session_turn.recipe

User-selectable prompt scaffolds picked from the composer (see
``app/models/recipe.py``). Sibling of ``skill``: same BUILTIN / CUSTOM
origin model (``user_id IS NULL`` ⇒ platform-wide), same uniqueness
shape (composite unique + partial-unique for the NULL-owner set + a
consistency CHECK). Unlike a skill there is no uploaded bundle — the
payload is the ``instructions`` text column, injected per-turn as an
axon system-reminder.

Also adds the nullable ``session_turn.recipe`` JSONB column holding the
pinned per-turn snapshot (``{id, display_name, instructions}``) written
by the backend at enqueue time.

Both changes are additive — a new table, a new enum, and a nullable
column default ``NULL`` — so existing rows are untouched.

Revision ID: f2a4c6e8b1d3
Revises: d4f7a2c9e1b6
Create Date: 2026-06-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f2a4c6e8b1d3"
down_revision: Union[str, Sequence[str], None] = "d4f7a2c9e1b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ORIGIN_NAME = "recipe_origin"
_ORIGIN_VALUES = ("BUILTIN", "CUSTOM")


def upgrade() -> None:
    # Raw SQL CREATE TYPE, then reference it with create_type=False on
    # the column — matches the project convention (see payment_order).
    op.execute(
        f"CREATE TYPE {_ORIGIN_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _ORIGIN_VALUES)})"
    )

    op.create_table(
        "recipe",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "origin",
            postgresql.ENUM(*_ORIGIN_VALUES, name=_ORIGIN_NAME, create_type=False),
            nullable=False,
            server_default=sa.text("'CUSTOM'"),
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"),
        ),
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
    )

    op.create_unique_constraint(
        "uq_recipe_user_id_name", "recipe", ["user_id", "name"],
    )
    op.create_index(
        "uq_recipe_builtin_name",
        "recipe",
        ["name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.create_check_constraint(
        "ck_recipe_origin_user_id_consistent",
        "recipe",
        "(origin = 'BUILTIN' AND user_id IS NULL) "
        "OR (origin = 'CUSTOM' AND user_id IS NOT NULL)",
    )

    op.add_column(
        "session_turn",
        sa.Column("recipe", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_turn", "recipe")
    op.drop_constraint(
        "ck_recipe_origin_user_id_consistent", "recipe", type_="check",
    )
    op.drop_index("uq_recipe_builtin_name", table_name="recipe")
    op.drop_constraint("uq_recipe_user_id_name", "recipe", type_="unique")
    op.drop_table("recipe")
    op.execute(f"DROP TYPE IF EXISTS {_ORIGIN_NAME}")
