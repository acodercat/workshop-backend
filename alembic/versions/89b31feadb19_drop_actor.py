"""drop actor

The ``actor`` table (``a7c3f1d9e4b2``) and its CRUD endpoints shipped
ahead of any client: no frontend, worker or other code read or wrote it,
and the tables it was meant to anchor (``session.actor_id``,
``actor_file``, ``session_record``) never arrived. Removed with its
code rather than kept as schema nothing maintains.

**Data.** Dropping the table discards its rows. Confirm
``SELECT count(*) FROM actor`` is 0 on each database before upgrading;
the downgrade recreates the table empty.

Revision ID: 89b31feadb19
Revises: c4cba460bb50
Create Date: 2026-10-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "89b31feadb19"
down_revision: Union[str, Sequence[str], None] = "c4cba460bb50"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_actor_user_id_updated_at", table_name="actor")
    op.drop_index("uq_actor_user_id_name", table_name="actor")
    op.drop_table("actor")


def downgrade() -> None:
    op.create_table(
        "actor",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("avatar_key", sa.String(), nullable=True, unique=True),
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
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_actor_user_id_name",
        "actor",
        ["user_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_actor_user_id_updated_at",
        "actor",
        ["user_id", sa.text("updated_at DESC")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
