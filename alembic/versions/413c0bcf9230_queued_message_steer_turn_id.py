"""queued_message.steer_turn_id and steer_taken: steering is for one turn

``steer`` asked for the message to reach whichever turn ran next, and stayed
set once the turn it was meant for had ended; ``steered_turn_id`` named the
turn that took it. ``steer_turn_id`` names the turn the message is to reach
instead, so the request ends with that turn and an untaken message waits as
any other — and only that turn can take it, so the take is a flag,
``steer_taken``. Only a take carries over, to the turn that made it: which
turn an untaken ``steer`` was meant for was never recorded, so it is
dropped rather than sent into whatever turn happens to be open now.

Revision ID: 413c0bcf9230
Revises: e1f4a7c2b9d3
Create Date: 2026-11-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "413c0bcf9230"
down_revision: Union[str, Sequence[str], None] = "e1f4a7c2b9d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("queued_message", sa.Column("steer_turn_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "queued_message",
        sa.Column("steer_taken", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_foreign_key(
        "queued_message_steer_turn_id_fkey",
        "queued_message",
        "session_turn",
        ["steer_turn_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE queued_message
           SET steer_turn_id = steered_turn_id,
               steer_taken = true
         WHERE steered_turn_id IS NOT NULL
        """
    )
    op.drop_constraint("queued_message_steered_turn_id_fkey", "queued_message", type_="foreignkey")
    op.drop_column("queued_message", "steered_turn_id")
    op.drop_column("queued_message", "steer")


def downgrade() -> None:
    op.add_column(
        "queued_message",
        sa.Column("steer", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("queued_message", sa.Column("steered_turn_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "queued_message_steered_turn_id_fkey",
        "queued_message",
        "session_turn",
        ["steered_turn_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # A request whose turn has ended is an ordinary queued message here, and
    # ``steer`` would send it into whichever turn runs next: only an open
    # turn's request goes back as one.
    op.execute(
        """
        UPDATE queued_message
           SET steer = EXISTS (
                   SELECT 1 FROM session_turn st
                    WHERE st.id = queued_message.steer_turn_id
                      AND st.status IN ('PENDING', 'RUNNING')
               ),
               steered_turn_id = CASE WHEN steer_taken THEN steer_turn_id END
        """
    )
    op.drop_constraint("queued_message_steer_turn_id_fkey", "queued_message", type_="foreignkey")
    op.drop_column("queued_message", "steer_taken")
    op.drop_column("queued_message", "steer_turn_id")
