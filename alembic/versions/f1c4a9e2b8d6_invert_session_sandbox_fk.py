"""invert session ↔ sandbox direction: sandbox.session_id

Previously ``session.sandbox_id`` pointed at the (single, current)
sandbox serving the session. Reaper-driven recreates overwrote the
column, dropping any history of which sandboxes a session had used.

This migration moves the column to the other side: every ``sandbox``
row records the ``session_id`` it serves. Multiple sandbox rows per
session over time are now first-class (the historical record).

No DB-level FK — the column is application-managed (worker writes,
worker reads). Kept that way intentionally for flexibility (no
CASCADE footgun, no constraint cost on the hot insert path).

Migration steps (single transaction):
  1. Add ``sandbox.session_id`` (nullable for the backfill window).
  2. Backfill from ``session.sandbox_id`` reverse-mapping — every
     sandbox a session currently points at gets that session's id.
  3. DELETE orphan sandbox rows: rows that no session points at.
     They're historical recreates whose original session association
     was lost the moment ``session.sandbox_id`` got overwritten —
     unrecoverable, never read by any code path. Verified safe.
  4. Promote ``sandbox.session_id`` to NOT NULL.
  5. Index on ``session_id`` for the worker's "latest non-TERMINATED
     sandbox per session" lookup in claim_next_run.
  6. Drop ``session.sandbox_id`` and its FK.

Revision ID: f1c4a9e2b8d6
Revises: e3a8b6f4c1d2
Create Date: 2026-05-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f1c4a9e2b8d6"
down_revision: Union[str, Sequence[str], None] = "e3a8b6f4c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add nullable for backfill.
    op.add_column(
        "sandbox",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # 2. Backfill: each sandbox a session currently points at gets that
    #    session's id stamped on its session_id column.
    op.execute(
        """
        UPDATE sandbox sb
           SET session_id = s.id
          FROM session s
         WHERE s.sandbox_id = sb.id
        """
    )

    # 3. Delete orphans — sandbox rows no session currently points at.
    #    These are historical recreates whose original session
    #    association was overwritten on ``session.sandbox_id`` and is
    #    unrecoverable. No code reads them.
    op.execute("DELETE FROM sandbox WHERE session_id IS NULL")

    # 4. Promote to NOT NULL.
    op.alter_column("sandbox", "session_id", nullable=False)

    # 5. Index for the worker's per-session lookup. ``claim_next_run``
    #    will run a subquery that filters by session_id + status, so
    #    a plain B-tree on session_id is enough (sandbox count per
    #    session is tiny — handful of rows max).
    op.create_index(
        "ix_sandbox_session_id",
        "sandbox",
        ["session_id"],
    )

    # 6. Drop the inverse pointer + its FK.
    #    ``session.sandbox_id`` was a string FK to sandbox.id with
    #    ON DELETE SET NULL. Dropping the column drops the constraint.
    op.drop_column("session", "sandbox_id")


def downgrade() -> None:
    # Restore session.sandbox_id with the same shape as before.
    op.add_column(
        "session",
        sa.Column(
            "sandbox_id",
            sa.String(length=64),
            sa.ForeignKey("sandbox.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Backfill from sandbox.session_id (most recent active row per session
    # wins, matching the pre-migration semantics where the column held
    # whichever sandbox was last assigned).
    op.execute(
        """
        UPDATE session s
           SET sandbox_id = (
             SELECT sb.id
               FROM sandbox sb
              WHERE sb.session_id = s.id
                AND sb.status <> 'TERMINATED'
              ORDER BY sb.created_at DESC
              LIMIT 1
           )
        """
    )

    op.drop_index("ix_sandbox_session_id", table_name="sandbox")
    op.drop_column("sandbox", "session_id")
