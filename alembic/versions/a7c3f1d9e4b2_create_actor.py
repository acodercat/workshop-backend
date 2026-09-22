"""create actor

The ``actor`` table — a user-owned, persistent AI worker identity
(数字员工). Identity only: name, a human-facing description, a
model-facing instructions charter, and an optional uploaded-avatar OSS
key. Tasks, files, and work records arrive in later revisions as
tables *referencing* this one (``session.actor_id``, ``actor_file``,
``session_record``); nothing else reads or writes ``actor`` yet, so
this revision is deploy-order free.

Both indexes are partial on ``deleted_at IS NULL``: the unique one so
a soft-deleted actor's name can be reused, the list one because its
only consumer (the actor list) always filters tombstones out.

**hkgai note — the parent differs from upstream on purpose.** Upstream this
revision hangs off ``a1f4c7e02b31``, but this branch's chain grew past that
point on its own (``a3f9c1d7e2b4`` re-pointed, ``c8f2a5e1d94b``,
``b6c1e83f27d4``, ``c5e1a8f3b92d``, ``d8a4f1c6e290``) and the deployed
database may already stand on any of them. Copied verbatim it would open a
second head rooted behind that database. It is re-pointed at this branch's
head instead, with the upstream **id** kept so the two chains stay
comparable — do not "correct" ``down_revision`` back on a later sync.

Revision ID: a7c3f1d9e4b2
Revises: d8a4f1c6e290
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3f1d9e4b2"
down_revision: Union[str, Sequence[str], None] = "d8a4f1c6e290"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_actor_user_id_updated_at", table_name="actor")
    op.drop_index("uq_actor_user_id_name", table_name="actor")
    op.drop_table("actor")
