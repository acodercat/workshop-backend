"""rename sandbox_status 'RUNNING' -> 'LIVE'

The ``sandbox`` status enum models an app-level lifecycle, not a pure
container-state mirror (``REAPING`` is the reaper's claim, not a
container process state). ``RUNNING`` read ambiguously — as "a turn is
executing" rather than its actual meaning, "the container is up and
usable". ``LIVE`` names that usable state directly and reads cleanly
against the terminal ``REAPING`` / ``TERMINATED`` states.

``ALTER TYPE ... RENAME VALUE`` relabels the value in place — existing
rows update transparently, no data migration — and, unlike ``ADD VALUE``,
runs inside the transaction alembic wraps each migration in (no
autocommit block needed). It is exactly reversible, so the downgrade is
a true inverse. The rename is atomic: there is no window where both
labels are valid, so deploy this together with the worker + backend that
emit ``'LIVE'`` (same coordinated-deploy step as the REAPING migration).

Revision ID: f3b8d1e6c9a2
Revises: a1c4e7b2f9d3
Create Date: 2026-06-12

"""
from typing import Sequence, Union

from alembic import op


revision: str = "f3b8d1e6c9a2"
down_revision: Union[str, Sequence[str], None] = "a1c4e7b2f9d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE sandbox_status RENAME VALUE 'RUNNING' TO 'LIVE'")


def downgrade() -> None:
    op.execute("ALTER TYPE sandbox_status RENAME VALUE 'LIVE' TO 'RUNNING'")
