"""strip active_form from session_context.todos

axon dropped ``TodoItem.active_form`` (the present-continuous label) — the
checklist now carries only ``content`` + ``status``. New writes already omit
the key; this backfills existing rows by removing ``active_form`` from every
item in the ``todos`` JSONB array, so no stale key lingers in stored data.

Only rows whose ``todos`` actually contain the key are touched, and item order
is preserved (``WITH ORDINALITY`` + ``ORDER BY``).

Revision ID: d8c2f4a6e9b1
Revises: c3f0a7d2e1b9
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8c2f4a6e9b1"
down_revision: Union[str, Sequence[str], None] = "c3f0a7d2e1b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE session_context AS sc
        SET todos = COALESCE(
            (
                SELECT jsonb_agg(elem - 'active_form' ORDER BY ord)
                FROM jsonb_array_elements(sc.todos) WITH ORDINALITY AS t(elem, ord)
            ),
            '[]'::jsonb
        )
        WHERE jsonb_path_exists(sc.todos, '$[*].active_form')
        """
    )


def downgrade() -> None:
    # Irreversible: the present-continuous strings are discarded, not archived,
    # so there is nothing to restore. No-op.
    pass
