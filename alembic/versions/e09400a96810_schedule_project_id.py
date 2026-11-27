"""schedule.project_id: the project each firing's session is created in

NULL for every existing schedule, which keeps them firing outside any
project as they do today. The index is plain rather than ``CONCURRENTLY``:
``schedule`` holds at most a hundred rows per user.

Revision ID: e09400a96810
Revises: 413c0bcf9230
Create Date: 2026-11-23

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e09400a96810"
down_revision: Union[str, Sequence[str], None] = "413c0bcf9230"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("schedule", sa.Column("project_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "schedule_project_id_fkey",
        "schedule",
        "project",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_schedule_project_id",
        "schedule",
        ["project_id"],
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_schedule_project_id", table_name="schedule")
    op.drop_constraint("schedule_project_id_fkey", "schedule", type_="foreignkey")
    op.drop_column("schedule", "project_id")
