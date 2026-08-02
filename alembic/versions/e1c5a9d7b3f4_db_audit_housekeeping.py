"""db-audit housekeeping: withdrawal ledger_id NOT NULL, index tidy-up

Three small independent leftovers from the 2026-07 schema audit, batched
because each is a one-liner:

1. ``withdrawal_request.ledger_id`` SET NOT NULL. Leaving it nullable
   invited exactly the drift the up-front WITHDRAW debit exists to
   prevent (a request whose funds were never held). Same reasoning as
   ``b9d5e3a1c7f4`` (credit_reservation.turn_id). The writer
   (``request_withdrawal``) used to flush the request *before* the
   ledger debit (chicken-and-egg on the ledger's back-reference), which
   would have violated this at INSERT time — it was reordered in the
   same change (client-side request id → ledger first → request
   inserted with ``ledger_id`` set), and deploy.sh applies migrations
   only after the code sync with both processes stopped, so the old
   writer never runs against the NOT NULL column. Pre-check: ``SELECT
   count(*) FROM withdrawal_request WHERE ledger_id IS NULL`` must be 0;
   ``SET NOT NULL`` fails loudly on the first violating row otherwise.
   Brief ACCESS EXCLUSIVE to revalidate, no rewrite — sub-second on this
   small table.

2. Drop ``ix_message_feedback_message_id``. Redundant since creation:
   the composite UNIQUE ``uq_message_feedback_user (message_id,
   user_id)`` has ``message_id`` leading, so its backing index already
   serves the FK CASCADE scan and every message-scoped read. Pure write
   amplification.

3. Add ``ix_user_skill_preference_skill_id``. The composite PK is
   ``(user_id, skill_id)`` — ``skill_id`` second — so the ``ON DELETE
   CASCADE`` from ``skill`` (a real API path: deleting a CUSTOM skill)
   seq-scans this table. Every other FK in the schema follows the
   "Postgres doesn't auto-index FK columns" discipline; this one was
   missed. Tiny table today; the index keeps it honest as platform-skill
   toggles accrue one row per user x skill.

Revision ID: e1c5a9d7b3f4
Revises: d5b1f7c3a9e2
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op


revision: str = "e1c5a9d7b3f4"
down_revision: Union[str, Sequence[str], None] = "d5b1f7c3a9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("withdrawal_request", "ledger_id", nullable=False)
    op.drop_index("ix_message_feedback_message_id", table_name="message_feedback")
    op.create_index(
        "ix_user_skill_preference_skill_id", "user_skill_preference", ["skill_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_skill_preference_skill_id", table_name="user_skill_preference",
    )
    op.create_index(
        "ix_message_feedback_message_id", "message_feedback", ["message_id"],
    )
    op.alter_column("withdrawal_request", "ledger_id", nullable=True)
