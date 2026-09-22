"""schedule: drop last_run_at

The column recorded when an occurrence last reached the agent, and the only
thing that read it was the management card — which showed it beside the
*latest* occurrence's outcome. The two describe different occurrences as
soon as one is skipped: yesterday's run time next to today's "skipped: out of
credits". The card now reads the latest ``schedule_run`` whole, time and
outcome together, so the column has no reader left and is dropped rather
than kept in step with a table that already holds the answer.

No data is lost that is not still derivable: every value it held is the
creation time of a ``schedule_run`` with a session.

Revision ID: a9e4c2d71f38
Revises: e1c6a8d40b73
Create Date: 2026-09-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9e4c2d71f38"
down_revision: Union[str, Sequence[str], None] = "e1c6a8d40b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("schedule", "last_run_at")


def downgrade() -> None:
    op.add_column(
        "schedule",
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # Restored from the history the column duplicated, so a downgrade does not
    # hand the old card a blank where it used to have a time.
    op.execute(
        """
        UPDATE schedule
           SET last_run_at = latest.created_at
          FROM (
                SELECT schedule_id, max(created_at) AS created_at
                  FROM schedule_run
                 WHERE session_id IS NOT NULL
                 GROUP BY schedule_id
               ) AS latest
         WHERE latest.schedule_id = schedule.id
        """
    )
