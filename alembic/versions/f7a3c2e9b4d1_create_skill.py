"""skill: per-user uploaded SKILL.md bundle metadata

Backs the upload-your-own-skill feature. The bundle itself (.zip)
lives in OSS at ``bundle_key``; this row carries the picker metadata
(name / description) and the enable toggle the worker honors when
staging per-user skills into the sandbox at session start.

``origin`` distinguishes platform-shipped BUILTIN skills (user_id
NULL, visible to everyone) from user-uploaded CUSTOM skills (user_id
set, scoped to the uploader).

Revision ID: f7a3c2e9b4d1
Revises: b9d5e3a1c7f4
Create Date: 2026-05-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f7a3c2e9b4d1"
down_revision: Union[str, Sequence[str], None] = "b9d5e3a1c7f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    skill_origin = postgresql.ENUM(
        "BUILTIN", "CUSTOM", name="skill_origin", create_type=False,
    )
    skill_origin.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "skill",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "origin", skill_origin, nullable=False,
            server_default=sa.text("'CUSTOM'"),
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=False),
        sa.Column("bundle_key", sa.String(), nullable=False),
        sa.Column("bundle_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bundle_key", name="uq_skill_bundle_key"),
        sa.UniqueConstraint("user_id", "name", name="uq_skill_user_id_name"),
    )
    # BUILTIN skills have user_id=NULL → the composite unique above
    # won't prevent duplicates (NULL ≠ NULL in SQL). Partial index
    # enforces name uniqueness within the BUILTIN set.
    op.create_index(
        "uq_skill_builtin_name",
        "skill",
        ["name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("skill")
    op.execute("DROP TYPE IF EXISTS skill_origin")
