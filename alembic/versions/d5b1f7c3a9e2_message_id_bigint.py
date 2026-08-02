"""message.id (+ message_feedback.message_id) Integer -> BigInteger

``message`` is the highest-volume append table (>= 2 rows per turn:
the USER insert in ``queue_user_message`` plus the worker's ASSISTANT
projection), yet kept a 2.1B-ceiling ``integer`` PK while the project's
own convention (stated in ``organization.py``) gives high-volume append
tables — ledger, session_turn, session_files — ``BigInteger``. Widen it
now, while the table is small and the rewrite is sub-second; done later
it's a long ACCESS EXCLUSIVE full-table rewrite on the hottest table.
``message_feedback.message_id`` (the FK) widens in the same migration
so the pair can't drift.

Order matters only for symmetry: each ``ALTER COLUMN ... TYPE bigint``
takes ACCESS EXCLUSIVE and rewrites its table; Postgres revalidates the
FK automatically (int4/int8 share a btree operator family, so even the
mid-state is consistent). deploy.sh stops both processes before
migrating, so nothing contends for the lock.

The PK's sequence needs no change — Postgres sequences are already
64-bit; only the column type capped it.

Downgrade narrows back (fails if any id exceeds 2^31-1, which is the
point of the exercise).

Revision ID: d5b1f7c3a9e2
Revises: c8a2e6b4d1f9
Create Date: 2026-07-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d5b1f7c3a9e2"
down_revision: Union[str, Sequence[str], None] = "c8a2e6b4d1f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "message_feedback", "message_id",
        type_=sa.BigInteger(), existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "message", "id",
        type_=sa.BigInteger(), existing_type=sa.Integer(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "message", "id",
        type_=sa.Integer(), existing_type=sa.BigInteger(),
        existing_nullable=False,
    )
    op.alter_column(
        "message_feedback", "message_id",
        type_=sa.Integer(), existing_type=sa.BigInteger(),
        existing_nullable=False,
    )
