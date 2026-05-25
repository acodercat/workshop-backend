"""skill: per-user uploaded SKILL.md bundle metadata

Backs the upload-your-own-skill feature. The bundle itself (.zip)
lives in OSS at ``bundle_key``; this row carries the picker metadata
(name / description) and the enable toggle the worker honors when
staging per-user skills into the sandbox at session start.

Revision ID: f7a3c2e9b4d1
Revises: b9d5e3a1c7f4
Create Date: 2026-05-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7a3c2e9b4d1"
down_revision: Union[str, Sequence[str], None] = "b9d5e3a1c7f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skill",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
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
        # Composite unique covers ``WHERE user_id = ?`` queries (picker
        # list, per-session staging) AND the FK CASCADE — no separate
        # ``user_id`` index needed.
        sa.UniqueConstraint("user_id", "name", name="uq_skill_user_id_name"),
    )


def downgrade() -> None:
    op.drop_table("skill")
