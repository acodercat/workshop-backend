"""session_turn interim usage columns for the budget enforcer

``interim_input_tokens`` / ``interim_output_tokens`` — cumulative tokens
consumed so far in a running turn, refreshed by the worker's lease heartbeat
(an OCS-style "interim update"). Read only by the backend's budget enforcer
to price in-flight turns; settle keeps trusting the fenced terminal
``input_tokens`` / ``output_tokens`` exclusively.

Nullable, no backfill (NULL = "no report yet"), no index — the enforcer
reaches these rows through the OPEN-reservation partial index, never by
scanning these columns. Metadata-only ALTER; instant.

Revision ID: f2b8d6c4a1e7
Revises: e7c4a9b2f1d8
Create Date: 2026-07-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f2b8d6c4a1e7"
down_revision: Union[str, Sequence[str], None] = "e7c4a9b2f1d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_turn",
        sa.Column("interim_input_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "session_turn",
        sa.Column("interim_output_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_turn", "interim_output_tokens")
    op.drop_column("session_turn", "interim_input_tokens")
