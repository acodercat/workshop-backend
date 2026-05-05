"""add session.model_id

Records which model each session was created with. Existing rows are
backfilled to ``claude-opus-4-6`` (the only model in use prior to this
migration). The ``server_default`` is dropped right after backfill —
new rows must specify ``model_id`` explicitly. The application reads
its default from ``settings.default_session_model_id`` at insert time.

Revision ID: 004dd48dc4a1
Revises: a3e1c02b8f45
Create Date: 2026-04-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "004dd48dc4a1"
down_revision: Union[str, Sequence[str], None] = "a3e1c02b8f45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session",
        sa.Column(
            "model_id",
            sa.String(length=64),
            nullable=False,
            server_default="claude-opus-4-6",
        ),
    )
    # Drop the DB-level default. New inserts must supply ``model_id``;
    # the app picks it up from ``settings.default_session_model_id``.
    op.alter_column("session", "model_id", server_default=None)


def downgrade() -> None:
    op.drop_column("session", "model_id")
