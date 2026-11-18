"""session_turn.timezone: the zone the user's client sent the message from

Read by the worker's ``create_schedule`` tool, so a task the agent schedules
runs on the user's own clock. Nullable with no default: NULL is "nobody
reported one" (a scheduled run, an older client), so no row of
``session_turn`` is rewritten.

Revision ID: a4e1c8f27b90
Revises: d861ee8ff0ea
Create Date: 2026-11-17

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4e1c8f27b90"
down_revision: Union[str, Sequence[str], None] = "d861ee8ff0ea"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("session_turn", sa.Column("timezone", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("session_turn", "timezone")
