"""One-off: backfill the ``session_site`` table from ``message.blocks``.

Published sites are recorded going forward by the worker
(``SessionSiteRepository``), but sites published before that landed have
no row. Every publish lives in a ``tool_result`` block
(``tool = 'publish_site'``) whose ``metadata`` carries ``slug`` (and
optional ``title``); this lifts each into ``session_site``, taking
``session_id`` from the message and ``user_id`` from that session's
owner, and stamping ``created_at`` with the publishing message's time
(not ``now()``) so the record reflects when it was actually published.

Careful / idempotent:
- ``ON CONFLICT (slug) DO NOTHING`` skips any slug already recorded
  (worker-written rows, or a previous run) — safe to re-run.
- ``DISTINCT ON (slug)`` keeps the earliest occurrence, so a slug that
  somehow appears in more than one message inserts exactly once (and
  never twice within this statement).
- INNER ``JOIN session`` means a publish whose session row is gone is
  skipped rather than failing the NOT-NULL/FK insert.
- ``blocks`` is guarded to an array; non-array rows contribute nothing.

Assumes the ``publish_id`` -> ``slug`` rename migration (90afe547ccf4)
has run, so historical blocks key on ``slug``.

Run:

    uv run python -m scripts.archive.backfill_session_site
"""

import asyncio

from sqlalchemy import text

from app.database import AsyncSessionLocal

_BACKFILL = text(
    """
    INSERT INTO session_site (slug, session_id, user_id, title, created_at)
    SELECT DISTINCT ON (b->'metadata'->>'slug')
        b->'metadata'->>'slug'             AS slug,
        m.session_id                       AS session_id,
        s.user_id                          AS user_id,
        left(b->'metadata'->>'title', 255) AS title,
        m.created_at                       AS created_at
    FROM message m
    JOIN session s ON s.id = m.session_id,
         jsonb_array_elements(
             CASE WHEN jsonb_typeof(m.blocks) = 'array'
                  THEN m.blocks ELSE '[]'::jsonb END
         ) AS b
    WHERE b->>'type' = 'tool_result'
      AND b->>'tool' = 'publish_site'
      AND coalesce(b->'metadata'->>'slug', '') <> ''
    ORDER BY b->'metadata'->>'slug', m.created_at
    ON CONFLICT (slug) DO NOTHING
    """
)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        before = (
            await db.execute(text("SELECT count(*) FROM session_site"))
        ).scalar()
        result = await db.execute(_BACKFILL)
        await db.commit()
        after = (
            await db.execute(text("SELECT count(*) FROM session_site"))
        ).scalar()
        print(
            f"session_site: {before} -> {after} rows "
            f"(+{after - before} backfilled; INSERT reported {result.rowcount})"
        )


if __name__ == "__main__":
    asyncio.run(main())
