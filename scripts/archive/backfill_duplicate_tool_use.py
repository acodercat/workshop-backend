"""Backfill historical duplicate ``tool_use_id`` rows in ``message.blocks``.

Counterpart to :mod:`scripts.diagnose_duplicate_tool_use`. The diagnostic
*finds* dirty rows; this script *rewrites* them so the n-th ``tool_use``
of a colliding id becomes ``${id}#${n-1}`` (matching what
``axon.Agent._call_model`` now does at write time after the Phase 2 fix).

The frontend's FIFO disambiguation already renders these dirty rows
correctly, so this backfill is **purely cosmetic / consistency**: it
makes historical data shape-identical to post-fix data, which keeps the
diagnostic clean going forward and lets future tools index by
``tool_use_id`` without dup handling.

Read-only by default — call with ``--apply`` to write. Wraps each row
in a transaction; processes ``--batch-size`` rows per commit so a
mid-run interrupt leaves the DB in a consistent partial state.

Scope:
  - **Intra-message duplicates only** (the ``tool_use_id`` appears
    multiple times in one ``message.blocks`` array). These are the
    rows that produce React duplicate-key warnings.
  - Cross-message duplicates (same ``tool_use_id`` across two messages
    of one session) are *not* touched — different React subtrees, no
    key collision; renaming would lose the provenance signal that
    Kimi reused the id across turns.

Pairing rule:
  Within one message, walk blocks in order. The n-th ``tool_use`` of
  raw id X gets renamed to ``X#${n}`` (n=0 stays as X). The n-th
  matching ``tool_result`` is renamed identically — preserves the
  same FIFO pairing the frontend uses for live + persisted reads.

Usage::

    # preview only (default — no writes)
    uv run python -m scripts.archive.backfill_duplicate_tool_use

    # apply renames in batches of 50
    uv run python -m scripts.archive.backfill_duplicate_tool_use --apply --batch-size 50

    # scope to one session id (uuid) — useful for staged rollout
    uv run python -m scripts.archive.backfill_duplicate_tool_use --session-id <uuid> --apply

    # focus on one message (preview only)
    uv run python -m scripts.archive.backfill_duplicate_tool_use --message-id 1706
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

import psycopg

from config import settings

# Find candidate messages — same SQL as the Pass 1 diagnostic, scoped to
# rows that have ANY intra-message duplicate. We then pull each candidate's
# full blocks array and recompute the rename in Python so the logic stays
# auditable.

SQL_FIND_DIRTY_MESSAGES = """
WITH dups AS (
    SELECT DISTINCT m.id
    FROM message m,
         jsonb_array_elements(m.blocks) AS b
    WHERE m.role = 'ASSISTANT'
      AND b->>'type' = 'tool_use'
      {filter_sql}
    GROUP BY m.id, b->>'tool_use_id'
    HAVING COUNT(*) > 1
)
SELECT m.id, m.session_id::text, m.created_at, m.blocks
FROM message m
WHERE m.id IN (SELECT id FROM dups)
ORDER BY m.created_at DESC
"""

SQL_LOAD_ONE_MESSAGE = """
SELECT id, session_id::text, created_at, blocks
FROM message
WHERE id = %(message_id)s
"""

SQL_UPDATE_BLOCKS = """
UPDATE message
SET blocks = %(blocks)s::jsonb
WHERE id = %(message_id)s
"""


# Rename core — pure function, unit-testable. Walks the blocks array in
# order, tracks per-raw-id occurrence counters separately for tool_use and
# tool_result, and rewrites ``tool_use_id`` in place when the count exceeds
# zero. Idempotent: re-running on already-renamed blocks is a no-op
# because the ``X#1`` shape itself doesn't collide with ``X``.


def rename_duplicates(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[int, str, str]]]:
    """Return ``(new_blocks, renames)`` where ``renames`` is a list of
    ``(array_index, old_tool_use_id, new_tool_use_id)`` tuples for
    every block whose id changed. ``new_blocks`` is a fresh list of
    fresh dicts — never mutates the input.

    Two FIFO counters keyed on the raw ``tool_use_id``: one for
    ``tool_use`` blocks (owners), one for ``tool_result`` blocks
    (consumers). The n-th block of a given type with a given raw id
    becomes ``${id}#${n}`` for n>0; the first occurrence stays as
    ``${id}``. Same scheme as :func:`axon.Agent._call_model` post-fix,
    so historical and post-fix data end up shape-identical.
    """
    out: list[dict[str, Any]] = []
    renames: list[tuple[int, str, str]] = []
    counts: dict[str, dict[str, int]] = {
        "tool_use": defaultdict(int),
        "tool_result": defaultdict(int),
    }

    for index, block in enumerate(blocks):
        block_type = block.get("type")
        raw_id = block.get("tool_use_id")
        type_counts = counts.get(block_type) if isinstance(block_type, str) else None
        if type_counts is None or not isinstance(raw_id, str):
            out.append(block)
            continue
        n = type_counts[raw_id]
        type_counts[raw_id] = n + 1
        if n == 0:
            out.append(block)
            continue
        new_id = f"{raw_id}#{n}"
        out.append({**block, "tool_use_id": new_id})  # copy-on-write
        renames.append((index, raw_id, new_id))
    return out, renames


def _build_filter(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if args.session_id is not None:
        clauses.append("AND m.session_id = %(session_id)s::uuid")
        params["session_id"] = args.session_id
    if args.days is not None:
        clauses.append("AND m.created_at > NOW() - %(days)s * INTERVAL '1 day'")
        params["days"] = args.days
    return " ".join(clauses), params


def _print_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _summarize_renames(renames: list[tuple[int, str, str]]) -> str:
    if not renames:
        return "(no renames)"
    # Group by old → new for a compact summary; counts include both
    # tool_use and tool_result blocks that share the same renamed id.
    grouped: dict[tuple[str, str], int] = defaultdict(int)
    for _, old, new in renames:
        grouped[(old, new)] += 1
    return ", ".join(f"{old}→{new} ({n}x)" for (old, new), n in sorted(grouped.items()))


def _process_message(
    cur: psycopg.Cursor,
    message_id: int,
    session_id: str,
    blocks: list[dict[str, Any]],
    *,
    apply: bool,
) -> int:
    """Process one message; return the count of renames performed (or
    that would be performed in dry-run). Writes only when ``apply`` is
    true."""
    new_blocks, renames = rename_duplicates(blocks)
    if not renames:
        return 0
    summary = _summarize_renames(renames)
    prefix = "APPLY" if apply else "DRY"
    print(
        f"  [{prefix}] message {message_id} (session {session_id[:8]}…): "
        f"{len(renames)} block(s) renamed — {summary}"
    )
    if apply:
        cur.execute(
            SQL_UPDATE_BLOCKS,
            {"message_id": message_id, "blocks": json.dumps(new_blocks)},
        )
    return len(renames)


def _run_message_detail(cur: psycopg.Cursor, message_id: int) -> int:
    """Show the proposed renames for one message in full, never writes."""
    cur.execute(SQL_LOAD_ONE_MESSAGE, {"message_id": message_id})
    row = cur.fetchone()
    _print_header(f"PREVIEW — message {message_id}")
    if row is None:
        print(f"  No message with id={message_id}.")
        return 1
    msg_id, session_id, created_at, blocks = row
    print(f"  message_id  = {msg_id}")
    print(f"  session_id  = {session_id}")
    print(f"  created_at  = {created_at}")
    print(f"  block_count = {len(blocks)}")
    new_blocks, renames = rename_duplicates(blocks)
    if not renames:
        print("\n  No duplicates to rename in this message. ✓")
        return 0
    print(f"\n  Proposed renames ({len(renames)}):")
    for index, old, new in renames:
        block = blocks[index]
        kind = block.get("type")
        print(f"    [block #{index}] type={kind!r}: {old!r} → {new!r}")
    print(
        "\n  Re-run without --message-id and pass --apply to write this "
        "(plus any other dirty messages in scope)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill duplicate tool_use_id rows in message.blocks. "
            "Read-only by default — pass --apply to write."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--session-id",
        help="UUID of one session to scope the backfill to. Defaults to all.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Lookback window in days. Default: scan all history (since "
        "this is a one-shot cleanup, not a periodic job).",
    )
    parser.add_argument(
        "--message-id",
        type=int,
        help="If set, preview the rename for this single message and exit "
        "(implies dry-run regardless of --apply).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the renames. Without this flag the script "
        "only previews what would change.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Commit every N messages. A mid-run Ctrl-C leaves the DB "
        "consistent up to the last batch boundary.",
    )
    args = parser.parse_args()

    psycopg_uri = settings.db_uri.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(psycopg_uri) as conn, conn.cursor() as cur:
        if args.message_id is not None:
            return _run_message_detail(cur, args.message_id)

        scope = args.session_id or "(all sessions)"
        window = f"last {args.days} days" if args.days else "all history"
        mode = "APPLY (writes will happen)" if args.apply else "DRY-RUN (no writes)"
        _print_header(f"SCOPE: {scope}    WINDOW: {window}    MODE: {mode}")

        filter_sql, params = _build_filter(args)
        cur.execute(SQL_FIND_DIRTY_MESSAGES.format(filter_sql=filter_sql), params)
        rows = cur.fetchall()

        if not rows:
            print("\n  No dirty messages found. ✓")
            return 0

        print(f"\n  Found {len(rows)} message(s) with intra-message duplicates.\n")
        total_renames = 0
        batch_renames = 0
        for batch_pos, (message_id, session_id, _, blocks) in enumerate(rows):
            n = _process_message(
                cur,
                message_id,
                session_id,
                blocks,
                apply=args.apply,
            )
            total_renames += n
            batch_renames += n
            if args.apply and (batch_pos + 1) % args.batch_size == 0:
                conn.commit()
                print(f"  -- committed batch ({batch_renames} renames) --")
                batch_renames = 0
        if args.apply and batch_renames:
            conn.commit()
            print(f"  -- committed final batch ({batch_renames} renames) --")
        elif not args.apply:
            # Roll back any read-locks; safer than an open transaction
            # if the script is left running interactively.
            conn.rollback()

        _print_header("SUMMARY")
        print(f"  Messages touched:  {len(rows)}")
        print(f"  Blocks renamed:    {total_renames}")
        if not args.apply:
            print(
                "\n  This was a DRY-RUN — nothing was written. Re-run with "
                "--apply to commit the renames above."
            )
        else:
            print(
                "\n  Verification: re-run "
                "``uv run python -m scripts.archive.diagnose_duplicate_tool_use --days 0``. "
                "Pass 1 should now report 0 hits."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
