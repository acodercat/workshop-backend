"""rename session_run → session_turn (treated as new table)

Breaking change. Drops the existing ``session_run`` table + its
``session_run_status`` enum and creates a fresh ``session_turn`` /
``session_turn_status`` pair with the same column shape. Existing
session_run rows (in-flight queue work + historical bookkeeping)
are discarded — the user accepted this in lieu of an in-place
``RENAME TO`` chain that would have to also rename PK / FK / index
constraints to keep names consistent.

Revision ID: b8d2f4a6c7e9
Revises: a3c7e9d1b5f4
Create Date: 2026-05-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b8d2f4a6c7e9"
down_revision: Union[str, Sequence[str], None] = "a3c7e9d1b5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VALUES = ("PENDING", "RUNNING", "COMPLETE", "FAILED")
_ENUM_NAME = "session_turn_status"


def upgrade() -> None:
    # Drop the old shape entirely. ``session_run_status`` enum has no
    # other column references, so DROP TYPE is safe right after the
    # table goes.
    op.drop_index("ix_session_run_claim", table_name="session_run")
    op.drop_index("ix_session_run_session_id", table_name="session_run")
    op.drop_table("session_run")
    op.execute("DROP TYPE IF EXISTS session_run_status")

    # Create the new shape from scratch — same columns, new names.
    op.execute(
        f"CREATE TYPE {_ENUM_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _VALUES)})"
    )
    op.create_table(
        "session_turn",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
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
            postgresql.ENUM(*_VALUES, name=_ENUM_NAME, create_type=False),
            nullable=False,
            server_default=sa.text(f"'PENDING'::{_ENUM_NAME}"),
        ),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
    op.create_index("ix_session_turn_session_id", "session_turn", ["session_id"])
    op.create_index(
        "ix_session_turn_claim",
        "session_turn",
        ["status", "lease_expires_at"],
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("ix_session_turn_claim", table_name="session_turn")
    op.drop_index("ix_session_turn_session_id", table_name="session_turn")
    op.drop_table("session_turn")
    op.execute(f"DROP TYPE IF EXISTS {_ENUM_NAME}")

    # Recreate session_run from scratch (data not preserved).
    op.execute(
        "CREATE TYPE session_run_status AS ENUM "
        "('PENDING', 'RUNNING', 'COMPLETE', 'FAILED')"
    )
    op.create_table(
        "session_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
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
            postgresql.ENUM(
                "PENDING", "RUNNING", "COMPLETE", "FAILED",
                name="session_run_status", create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'PENDING'::session_run_status"),
        ),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_session_run_session_id", "session_run", ["session_id"])
    op.create_index(
        "ix_session_run_claim", "session_run",
        ["status", "lease_expires_at"],
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )
