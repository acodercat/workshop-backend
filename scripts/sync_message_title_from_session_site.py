"""One-off: backfill ``metadata.title`` on publish_site blocks from session_site.

The site-preview card reads its label from the ``tool_result`` block's
``metadata.title``; sites published without a title have none, so the
card falls back to the bare URL. ``session_site.title`` was just filled
from each page's ``<title>`` (see backfill_session_site_title), so this
copies that value back into the persisted ``message.blocks`` for any
publish_site block still missing a title.

Careful / idempotent (mirrors the publish_id->slug migration):
- Only messages whose blocks mention ``publish_site`` are scanned
  (text prefilter), and within each, only the array elements that are a
  publish_site ``tool_result`` with an empty/absent title AND a matching
  ``session_site.title`` are rewritten. Every other block passes through
  unchanged; original order preserved via ``WITH ORDINALITY``.
- ``LEFT JOIN session_site`` on slug (unique) never multiplies rows.
- Re-run is a no-op: once a title is set the empty-title guard fails.
- ``blocks`` guarded to an array.

Dry-run by default; pass ``--apply`` to write.

    uv run python -m scripts.sync_message_title_from_session_site
    uv run python -m scripts.sync_message_title_from_session_site --apply
"""

import asyncio
import sys

from sqlalchemy import text

from app.database import AsyncSessionLocal

_COUNT = text(
    """
    SELECT count(*)
    FROM message m,
         jsonb_array_elements(
             CASE WHEN jsonb_typeof(m.blocks) = 'array'
                  THEN m.blocks ELSE '[]'::jsonb END
         ) AS b
    LEFT JOIN session_site ss ON ss.slug = b->'metadata'->>'slug'
    WHERE b->>'type' = 'tool_result'
      AND b->>'tool' = 'publish_site'
      AND coalesce(b->'metadata'->>'title', '') = ''
      AND ss.title IS NOT NULL
    """
)

_UPDATE = text(
    """
    UPDATE message m
    SET blocks = sub.new_blocks
    FROM (
        SELECT m2.id,
               jsonb_agg(
                   CASE
                       WHEN t.elem->>'type' = 'tool_result'
                        AND t.elem->>'tool' = 'publish_site'
                        AND coalesce(t.elem->'metadata'->>'title', '') = ''
                        AND ss.title IS NOT NULL
                       THEN jsonb_set(t.elem, '{metadata,title}', to_jsonb(ss.title))
                       ELSE t.elem
                   END
                   ORDER BY t.ord
               ) AS new_blocks
        FROM message m2
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(m2.blocks) = 'array'
                 THEN m2.blocks ELSE '[]'::jsonb END
        ) WITH ORDINALITY AS t(elem, ord)
        LEFT JOIN session_site ss ON ss.slug = t.elem->'metadata'->>'slug'
        WHERE m2.blocks::text LIKE '%publish_site%'
        GROUP BY m2.id
    ) sub
    WHERE m.id = sub.id
    """
)


async def main() -> None:
    apply = "--apply" in sys.argv
    async with AsyncSessionLocal() as db:
        n = (await db.execute(_COUNT)).scalar()
        print(f"publish_site blocks missing a title with a session_site.title: {n}")
        if not apply:
            print("DRY RUN — re-run with --apply to write.")
            return
        result = await db.execute(_UPDATE)
        await db.commit()
        remaining = (await db.execute(_COUNT)).scalar()
        print(f"updated messages: {result.rowcount}; blocks still missing title: {remaining}")


if __name__ == "__main__":
    asyncio.run(main())
