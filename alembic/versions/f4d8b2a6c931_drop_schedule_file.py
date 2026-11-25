"""drop schedule_file: scheduled tasks no longer carry attachments

The form's attachment picker is gone, and with it the only writer of this
table. Nothing reads it any more either: the dispatcher fires a task with
its prompt, skills and Connectors alone.

The OSS objects behind any remaining rows are left alone, as a deleted
schedule always left them — their orphan tag was cleared when attached, so
the lifecycle rule will not reap them; the sessions a task already fired
keep their own ``session_files`` bindings of them.

``downgrade`` restores the empty table, not its rows.

Revision ID: f4d8b2a6c931
Revises: a4e1c8f27b90
Create Date: 2026-11-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4d8b2a6c931"
down_revision: Union[str, Sequence[str], None] = "a4e1c8f27b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_schedule_file_user_id", table_name="schedule_file")
    op.drop_table("schedule_file")


def downgrade() -> None:
    op.create_table(
        "schedule_file",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("file_key", sa.String(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["schedule.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "file_key", name="uq_schedule_file_schedule_id_file_key"
        ),
    )
    op.create_index("ix_schedule_file_user_id", "schedule_file", ["user_id"])
