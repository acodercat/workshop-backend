"""One-off: correct ``session_files.origin`` rows mis-tagged as DELIVERABLE.

Two sources of historical mis-tagging, both leaking agent-side artifacts
into the file library (``GET /sessions/files`` lists UPLOAD + DELIVERABLE):

1. The deploy-gap default — while ``origin`` carried
   ``DEFAULT 'DELIVERABLE'`` (added by migration ``f1a7c3e9d2b6``,
   dropped by ``a3f8c1d6e4b9``), any insert without an explicit origin
   landed there.
2. The migration's key-shape backfill could only spot ``read_image``
   inputs whose key embeds the tool name (``functions.read_image:N``).
   Inputs whose tool_use_id is an opaque provider id (``call_…`` /
   ``toolu_…``) fell through to DELIVERABLE.

Fix, both scoped to ``origin = 'DELIVERABLE'`` so the worker's own tags
stay untouched:

- preview sidecars (``{name}.preview.pdf``) → INTERNAL by key shape (a
  sidecar has no tool_use block of its own).
- ``read_image`` inputs → INTERNAL by the **authoritative** source: the
  ``tool_use`` blocks in ``message.blocks``. The file_key's 3rd path
  segment is the tool_use_id; if a block with that id is a ``read_image``
  call, the file is an input regardless of the id's format.

Idempotent — safe to re-run.

    uv run python -m scripts.fix_session_file_origin
"""

import asyncio

from sqlalchemy import text

from app.database import AsyncSessionLocal

_PREVIEW_FIX = text(
    "UPDATE session_files SET origin = 'INTERNAL' "
    "WHERE origin = 'DELIVERABLE' AND file_key LIKE '%.preview.pdf'"
)

# A tool output whose tool_use_id (the file_key's 3rd path segment) belongs
# to a read_image call is an input the model looked at, not a deliverable.
# Matching on the tool name in message.blocks catches every id format
# (``functions.read_image:N`` / ``call_…`` / ``toolu_…``) the key alone
# can't classify.
_READ_IMAGE_FIX = text(
    "UPDATE session_files SET origin = 'INTERNAL' "
    "WHERE origin = 'DELIVERABLE' AND file_key LIKE 'outputs/%' "
    "AND split_part(file_key, '/', 3) IN ("
    "  SELECT block->>'tool_use_id' "
    "  FROM message, jsonb_array_elements(blocks) AS block "
    "  WHERE block->>'type' = 'tool_use' AND block->>'tool' = 'read_image'"
    ")"
)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        previews = (await db.execute(_PREVIEW_FIX)).rowcount
        read_images = (await db.execute(_READ_IMAGE_FIX)).rowcount
        await db.commit()
        print(f"preview sidecars  -> INTERNAL: {previews}")
        print(f"read_image inputs -> INTERNAL: {read_images}")

        rows = (
            await db.execute(
                text(
                    "SELECT origin, count(*) FROM session_files "
                    "GROUP BY origin ORDER BY count(*) DESC"
                )
            )
        ).all()
        print("origin distribution now:")
        for origin, count in rows:
            print(f"  {origin}: {count}")


if __name__ == "__main__":
    asyncio.run(main())
