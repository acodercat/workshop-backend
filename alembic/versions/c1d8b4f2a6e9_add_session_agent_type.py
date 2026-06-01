"""add session.agent_type enum (FULL / LITE)

Adds the per-session dispatch key the worker reads at claim time to pick
its agent harness: ``FULL`` = sandbox-backed agent, ``LITE`` = sandbox-free
Q&A + web search. Mirrors the project enum convention (typed Postgres
enum + UPPERCASE labels) used by ``session_status`` / ``sandbox_status``.

Existing rows are all ``FULL`` (the only mode that existed), so the column
is added NOT NULL with a temporary ``server_default`` of ``'FULL'`` to
backfill, then the default is dropped — matching ``session.model_id`` /
``session.status``, which carry no column default and are set explicitly
at ``create_session``.

Revision ID: c1d8b4f2a6e9
Revises: d4e9f1a6c3b7
Create Date: 2026-05-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d8b4f2a6e9"
down_revision: Union[str, Sequence[str], None] = "d4e9f1a6c3b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VALUES = ("FULL", "LITE")
_ENUM_NAME = "agent_type"


def upgrade() -> None:
    op.execute(
        f"CREATE TYPE {_ENUM_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _VALUES)})"
    )
    # Temporary server_default backfills existing rows to FULL and satisfies
    # NOT NULL during ADD COLUMN; dropped right after so every INSERT sets
    # agent_type explicitly (matches model_id / status — no column default).
    op.add_column(
        "session",
        sa.Column(
            "agent_type",
            sa.Enum(*_VALUES, name=_ENUM_NAME, create_type=False),
            nullable=False,
            server_default=sa.text(f"'FULL'::{_ENUM_NAME}"),
        ),
    )
    op.alter_column("session", "agent_type", server_default=None)


def downgrade() -> None:
    op.drop_column("session", "agent_type")
    op.execute(f"DROP TYPE IF EXISTS {_ENUM_NAME}")
