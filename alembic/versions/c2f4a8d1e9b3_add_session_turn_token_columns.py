"""session_turn: add input_tokens / output_tokens

V2 of the credit subsystem moves authoritative token usage from the
ephemeral Redis stream (``agent.stopped`` event) into durable storage
on ``session_turn``. The worker writes these two columns in the same
fenced transaction that flips ``session_turn.status`` to ``COMPLETE``
or ``CANCELLED``, so the moment that row is terminal the usage is
already persisted alongside it.

Backend's settle path then reads these columns instead of the stream
tail — removing the Redis-deletion race that made V1 brittle and the
SSE eager hook that compensated for it. ``NULL`` semantics:

- ``COMPLETE`` / ``CANCELLED`` rows written by V2+ workers: filled in
  with the agent's reported token counts.
- ``FAILED`` rows (worker error path, recovery sweep on dead lease):
  left ``NULL`` on purpose — failure means refund, so the absence of
  usage data is the signal.
- Pre-V2 historical rows: ``NULL``. Any straggler reservations from
  before this migration get refunded by the lazy sweep / janitor.

Two ``int`` columns rather than ``bigint`` — token counts for a single
turn are bounded by context-window × max_output, both well under
2³¹ (largest current model context is ~1M tokens).

Revision ID: c2f4a8d1e9b3
Revises: b1c7e29f4d8a
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2f4a8d1e9b3"
down_revision: Union[str, Sequence[str], None] = "b1c7e29f4d8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_turn",
        sa.Column("input_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "session_turn",
        sa.Column("output_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_turn", "output_tokens")
    op.drop_column("session_turn", "input_tokens")
