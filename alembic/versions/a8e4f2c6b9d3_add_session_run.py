"""create session_run

DB-lease ownership row for one queued turn. The backend INSERTs a row
with ``status='PENDING'`` per user message; the worker atomically claims
it (``FOR UPDATE SKIP LOCKED + UPDATE``), holds the lease via periodic
renew, and writes the terminal state on success / failure.

Schema decisions:

- ``status`` is a typed Postgres enum (``session_run_status``) with
  UPPERCASE labels matching the worker's
  ``agent_worker.infra.database.SessionRunStatus`` member names — a
  closed three-way contract (Python enum / Postgres enum / wire).
- No ``user_id`` / ``model_id`` / ``sandbox_id`` cache columns —
  worker JOINs ``session`` to read them live, so the parent row is
  the single source of truth.
- Partial index ``ix_session_run_claim`` covers the hot scan path
  (``claim_next_run`` over ``PENDING`` + expired ``RUNNING``,
  recovery cron over ``RUNNING``); terminal rows are excluded so the
  index stays small.

Revision ID: a8e4f2c6b9d3
Revises: b74c1e2f9a30
Create Date: 2026-05-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a8e4f2c6b9d3"
down_revision: Union[str, Sequence[str], None] = "b74c1e2f9a30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VALUES = ("PENDING", "RUNNING", "COMPLETE", "FAILED")
_ENUM_NAME = "session_run_status"


def upgrade() -> None:
    # Raw SQL CREATE TYPE — sa.Enum's metadata-bound auto-create has
    # surprising interactions with create_table when create_type=False
    # is set on the column-side enum, so emit it explicitly here.
    op.execute(
        f"CREATE TYPE {_ENUM_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _VALUES)})"
    )

    op.create_table(
        "session_run",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column(
            "files",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "status",
            # Dialect-specific ENUM with create_type=False so SA doesn't
            # re-emit CREATE TYPE here (we did it above).
            postgresql.ENUM(*_VALUES, name=_ENUM_NAME, create_type=False),
            nullable=False,
            server_default=sa.text(f"'PENDING'::{_ENUM_NAME}"),
        ),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column(
            "lease_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_session_run_session_id",
        "session_run",
        ["session_id"],
    )
    op.create_index(
        "ix_session_run_claim",
        "session_run",
        ["status", "lease_expires_at"],
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("ix_session_run_claim", table_name="session_run")
    op.drop_index("ix_session_run_session_id", table_name="session_run")
    op.drop_table("session_run")
    op.execute(f"DROP TYPE IF EXISTS {_ENUM_NAME}")
