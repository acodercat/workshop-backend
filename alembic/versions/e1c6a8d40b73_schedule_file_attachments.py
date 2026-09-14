"""schedule attachments

``schedule_file`` is a scheduled task's attachment list: the uploads re-bound
to a fresh session every time the task fires. One row per (schedule, file);
both parents cascade.

**hkgai note — this file is edited on this branch, and only here.** Upstream
this revision also changes ``session_files``: ``UNIQUE(file_key)`` becomes
``UNIQUE(session_id, file_key)``, ``ix_session_files_file_key`` is created,
and the old ``session_files_file_key_key`` is dropped — so that one upload can
be bound to every session a task creates. This branch made exactly that
change first, as its own revision ``d8a4f1c6e290``, and on this branch
``d8a4f1c6e290`` is always an ancestor of this one. Running upstream's copy
here would fail on its first statement (the constraint already exists) and
roll the whole upgrade back. So the ``session_files`` half is removed from
both ``upgrade`` and ``downgrade``, and only the table this revision adds
remains.

Edited rather than guarded: the order is fixed, so a guard would inspect the
live schema to answer a question whose answer never changes here — and a
schema-inspecting guard stops ``alembic upgrade ... --sql`` rendering offline
(see ``e2b7d4f9a1c6``). Do not restore the upstream body on a later sync.

Revision ID: e1c6a8d40b73
Revises: b5e9d3f70a26
Create Date: 2026-09-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1c6a8d40b73"
down_revision: Union[str, Sequence[str], None] = "b5e9d3f70a26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_schedule_file_user_id", table_name="schedule_file")
    op.drop_table("schedule_file")
