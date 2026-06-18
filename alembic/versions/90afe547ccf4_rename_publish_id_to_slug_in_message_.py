"""rename publish_id to slug in message blocks

Data backfill: ``publish_site`` tool_result blocks historically stored
their identifier under ``metadata.publish_id``; the worker now emits
``metadata.slug`` (the per-publish subdomain label) and the frontend
keys on it — so sites published before that change stopped rendering.
This renames the key in place across persisted ``message.blocks`` so old
messages match the current format.

Surgical + safe on a large ``blocks`` JSONB:
- Only messages whose blocks contain ``publish_id`` are touched (verified
  ~153 rows, every occurrence at exactly ``block.metadata.publish_id`` —
  no nesting), via the ``LIKE`` prefilter.
- Within a touched message, only the array elements that actually carry
  ``metadata.publish_id`` are rewritten; every other block passes through
  unchanged and original order is preserved (``WITH ORDINALITY`` +
  ``jsonb_agg(... ORDER BY ord)``).
- Idempotent: after the rename no ``publish_id`` remains, so a re-run
  matches nothing. Only the key is renamed — the ``url`` value (old
  path-style) is intentionally left as-is.

Revision ID: 90afe547ccf4
Revises: e28ed13414c4
Create Date: 2026-06-18

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '90afe547ccf4'
down_revision: Union[str, Sequence[str], None] = 'e28ed13414c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE message m
        SET blocks = sub.new_blocks
        FROM (
            SELECT m2.id,
                   jsonb_agg(
                       CASE
                           WHEN (elem -> 'metadata') ? 'publish_id'
                           THEN jsonb_set(
                                    elem #- '{metadata,publish_id}',
                                    '{metadata,slug}',
                                    elem #> '{metadata,publish_id}'
                                )
                           ELSE elem
                       END
                       ORDER BY ord
                   ) AS new_blocks
            FROM message m2,
                 jsonb_array_elements(m2.blocks) WITH ORDINALITY AS t(elem, ord)
            WHERE m2.blocks::text LIKE '%publish_id%'
            GROUP BY m2.id
        ) sub
        WHERE m.id = sub.id
        """
    )


def downgrade() -> None:
    # Irreversible: after the rename, backfilled rows are indistinguishable
    # from sites published natively with ``slug``, so a blind
    # slug -> publish_id rename would corrupt post-migration publishes.
    pass
