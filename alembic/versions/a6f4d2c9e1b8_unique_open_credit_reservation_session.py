"""enforce one open credit reservation per session

Revision ID: a6f4d2c9e1b8
Revises: e8b2f4d7c1a9
Create Date: 2026-05-20
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a6f4d2c9e1b8"
down_revision: Union[str, Sequence[str], None] = "e8b2f4d7c1a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_INDEX = "ix_credit_reservation_open_session"
_NEW_INDEX = "uq_credit_reservation_open_session"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM credit_reservation
                WHERE state = 'OPEN'
                  AND session_id IS NOT NULL
                GROUP BY session_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'credit_reservation has duplicate OPEN rows per session';
            END IF;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_NEW_INDEX}
        ON credit_reservation (session_id)
        WHERE state = 'OPEN'
          AND session_id IS NOT NULL
        """
    )
    op.execute(f"DROP INDEX IF EXISTS {_OLD_INDEX}")


def downgrade() -> None:
    op.execute(
        f"""
        CREATE INDEX {_OLD_INDEX}
        ON credit_reservation (session_id)
        WHERE state = 'OPEN'
        """
    )
    op.execute(f"DROP INDEX IF EXISTS {_NEW_INDEX}")
