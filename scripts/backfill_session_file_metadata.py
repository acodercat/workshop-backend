"""One-off: backfill session_files.filename / mime_type / size_bytes.

These columns (added by migration ``b4e2d8f1a9c3``) are populated going
forward by the producers — the backend upload path and the worker
tool-output path — but historical rows are null. Every file's presentation
metadata already lives in ``message.blocks`` (the USER text block's
``files[]`` for uploads, the ``tool_result`` block's ``files[]`` for tool
outputs), so this lifts it onto ``session_files``.

Only fills rows where ``filename IS NULL`` — idempotent, and never clobbers
a value a producer already wrote. Run:

    uv run python -m scripts.backfill_session_file_metadata
"""

import asyncio

from sqlalchemy import text

from app.database import AsyncSessionLocal

_BACKFILL = text(
    """
    WITH file_meta AS (
      SELECT DISTINCT ON (f->>'file_key')
        f->>'file_key'             AS file_key,
        f->>'filename'             AS filename,
        f->>'mime_type'            AS mime_type,
        (f->>'size_bytes')::bigint AS size_bytes
      FROM message,
           jsonb_array_elements(blocks) AS b,
           jsonb_array_elements(
             CASE WHEN jsonb_typeof(b->'files') = 'array'
                  THEN b->'files' ELSE '[]'::jsonb END
           ) AS f
      WHERE f ? 'file_key'
      ORDER BY f->>'file_key'
    )
    UPDATE session_files sf
    SET filename = fm.filename,
        mime_type = fm.mime_type,
        size_bytes = fm.size_bytes
    FROM file_meta fm
    WHERE sf.file_key = fm.file_key
      AND sf.filename IS NULL
    """
)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(_BACKFILL)
        await db.commit()
        print(f"backfilled {result.rowcount} rows")

        stats = (
            await db.execute(
                text(
                    "SELECT count(*) FILTER (WHERE filename IS NOT NULL) "
                    "AS with_meta, count(*) AS total FROM session_files"
                )
            )
        ).first()
        print(f"session_files with metadata: {stats.with_meta} / {stats.total}")


if __name__ == "__main__":
    asyncio.run(main())
