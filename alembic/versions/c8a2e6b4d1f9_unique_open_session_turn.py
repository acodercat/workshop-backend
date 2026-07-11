"""enforce one non-terminal session_turn per session

"At most one PENDING/RUNNING turn per session" is load-bearing in two
places — ``queue_user_message`` 409-guards on ``session.status ==
RUNNING`` under the session row lock, and both recovery paths
(``recovery.fail_abandoned_turn`` backend-side, ``expire_stale_turns``
worker-side) rely on a session-scoped flip targeting exactly one row —
but until now it was enforced only by that application-level lock
discipline. Any future write path that inserts a turn without taking
the session lock (a retry script, an admin backfill, a second enqueue
entry point) could silently create two open turns, and recovery would
then terminalize both while settle mis-routes usage.

Partial unique index makes it a DB-enforced contract, the exact idiom
``uq_credit_reservation_open_session`` (``a6f4d2c9e1b8``) already uses
for the sibling one-OPEN-reservation invariant. The indexed set is tiny
(open turns only), so build time and carry cost are negligible.

Pre-check mirrors ``a6f4d2c9e1b8``: abort loudly if historical data
already violates the invariant (terminalize the duplicates by hand —
``UPDATE session_turn SET status='FAILED', termination_reason=
'worker_lost', terminated_at=now() WHERE id = ...`` on the stale one —
then re-run). Plain (non-CONCURRENT) build is fine: deploy.sh stops
both the web and background processes before migrating, so there are
no concurrent writers during the build.

Revision ID: c8a2e6b4d1f9
Revises: b2d6f1a8c4e3
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c8a2e6b4d1f9"
down_revision: Union[str, Sequence[str], None] = "b2d6f1a8c4e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX = "uq_session_turn_open_session"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM session_turn
                WHERE status IN ('PENDING', 'RUNNING')
                GROUP BY session_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'session_turn has multiple open turns for one session';
            END IF;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_INDEX}
        ON session_turn (session_id)
        WHERE status IN ('PENDING', 'RUNNING')
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
