"""Migrate historical ``error`` blocks in ``message.blocks`` from old
``{"type": "error", "message": "..."}`` shape to the new
``{"type": "error", "content": "..."}`` shape.

Companion to the cross-stack rename of ``AgentErrorEvent.message`` →
``content``. Newly produced error blocks already use ``content``; this
script rewrites the small number of rows persisted before the rename
so the frontend renders ``> ⚠️ <text>`` instead of ``> ⚠️ undefined``
on historical sessions that contain an error block.

Read-only by default — pass ``--apply`` to write. Wraps each updated
row in a single batched transaction.

Idempotent: rows where the error block already carries ``content`` (or
that don't have any old-format error blocks) are skipped by the SQL
filter, so re-running this script on a clean DB is a no-op.

Usage::

    # preview (default — no writes)
    uv run python -m scripts.migrate_error_block_field

    # apply the migration
    uv run python -m scripts.migrate_error_block_field --apply
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import psycopg

from config import settings


SQL_FIND_DIRTY = """
SELECT id, session_id::text, created_at, blocks
FROM message
WHERE EXISTS (
    SELECT 1
    FROM jsonb_array_elements(blocks) AS b
    WHERE b->>'type' = 'error'
      AND b ? 'message'
      AND NOT (b ? 'content')
)
ORDER BY id
"""

SQL_UPDATE_BLOCKS = """
UPDATE message
SET blocks = %(blocks)s::jsonb
WHERE id = %(message_id)s
"""

SQL_VERIFY_RESIDUAL = """
SELECT COUNT(*)
FROM message
WHERE EXISTS (
    SELECT 1
    FROM jsonb_array_elements(blocks) AS b
    WHERE b->>'type' = 'error'
      AND b ? 'message'
      AND NOT (b ? 'content')
)
"""


def migrate_blocks(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Pure rewrite: walk the blocks array and rename ``message`` → ``content``
    on any ``{"type": "error", "message": ...}`` block that doesn't already
    carry ``content``.

    Returns ``(new_blocks, renamed_count)``. Never mutates the input.
    """
    out: list[dict[str, Any]] = []
    renamed = 0
    for block in blocks:
        if (
            block.get("type") == "error"
            and "message" in block
            and "content" not in block
        ):
            rewritten = {k: v for k, v in block.items() if k != "message"}
            rewritten["content"] = block["message"]
            out.append(rewritten)
            renamed += 1
        else:
            out.append(block)
    return out, renamed


def _print_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate historical {type: error, message: ...} blocks to "
            "{type: error, content: ...}. Dry-run by default — pass --apply "
            "to write."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the migration. Without this flag the script "
             "only previews what would change.",
    )
    args = parser.parse_args()

    psycopg_uri = settings.db_uri.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(psycopg_uri) as conn, conn.cursor() as cur:
        cur.execute(SQL_FIND_DIRTY)
        rows = cur.fetchall()

        mode = "APPLY" if args.apply else "DRY-RUN"
        _print_header(f"Error-block field migration — {mode}")
        print(f"  Found {len(rows)} message row(s) with old-format error blocks")

        if not rows:
            print("\n  Nothing to migrate. ✓")
            return 0

        total_blocks_rewritten = 0
        for row in rows:
            message_id, session_id, created_at, blocks = row
            new_blocks, renamed = migrate_blocks(blocks)
            total_blocks_rewritten += renamed
            prefix = "APPLY" if args.apply else "DRY"
            print(
                f"  [{prefix}] message {message_id} "
                f"(session {session_id[:8]}…, {created_at:%Y-%m-%d %H:%M}): "
                f"{renamed} error block(s) migrated"
            )
            if args.apply:
                cur.execute(
                    SQL_UPDATE_BLOCKS,
                    {"message_id": message_id, "blocks": json.dumps(new_blocks)},
                )

        if args.apply:
            conn.commit()
            print(f"\n  Committed: {len(rows)} row(s), "
                  f"{total_blocks_rewritten} error block(s) total.")
            cur.execute(SQL_VERIFY_RESIDUAL)
            residual = cur.fetchone()[0]
            if residual == 0:
                print("  Verification: 0 old-format error blocks remain. ✓")
            else:
                print(f"  Verification: {residual} old-format error block(s) STILL remain ✗")
                return 1
        else:
            print(f"\n  Dry-run only — would migrate {total_blocks_rewritten} error "
                  f"block(s) across {len(rows)} message row(s).")
            print("  Re-run with --apply to write.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
