"""Diagnose duplicate ``tool_use_id`` entries in ``message.blocks``.

Tracks down the source of the React ``Encountered two children with the
same key, tool-functions.bash:23`` warning. The frontend reducer keys
each task-node part by ``tool-${tool_use_id}``; if the persisted
``message.blocks`` array carries two ``tool_use`` blocks with the same
``tool_use_id`` (or the same id is reused across two assistant messages
in one session), the renderer emits duplicate React keys.

Three diagnostic passes — each runs against the current DB and prints a
summary plus the worst offenders. Read-only: no UPDATE / DELETE / DDL.

Pass 1 — duplicates **within a single message's ``blocks``**
    Most direct match for the React warning. A nonzero hit count
    means the worker persisted two ``tool_use`` rows with the same
    ``tool_use_id`` in one assistant turn — likely axon yielded
    two ``AgentToolUseEvent``s with the same id (provider quirk:
    Kimi K2's ``functions.<tool>:<seq>`` counter occasionally
    repeats), and ``build_blocks`` appended both blindly.

Pass 2 — duplicates **across messages of the same session**
    The same ``tool_use_id`` appearing in two distinct assistant
    messages of one session. Suggests one of:
    - axon's deterministic id counter spans turns and the model
      resumed an earlier id (rare),
    - the same turn was committed twice (lease race; two workers
      raced to persist after a stale lease was reclaimed),
    - manual data restore stitched the same export in twice.

Pass 3 — message-by-message detail dump
    Walks the messages flagged by Pass 1 and prints each duplicate
    block's full JSON so the operator can see whether the dupe rows
    are byte-identical (replay/race) or differ (e.g. one has args,
    the other empty — different model attempts after compaction).

Usage::

    # default: last 14 days, summary + top 20 offenders
    uv run python -m scripts.diagnose_duplicate_tool_use

    # focus on one session (uuid)
    uv run python -m scripts.diagnose_duplicate_tool_use --session-id <uuid>

    # change the lookback window
    uv run python -m scripts.diagnose_duplicate_tool_use --days 30

    # dump full block JSON for a specific message id (skips passes 1+2)
    uv run python -m scripts.diagnose_duplicate_tool_use --message-id 4321
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import psycopg

from config import settings


# ---------------------------------------------------------------------------
# SQL — read-only. The ``%(filter_sql)s`` placeholder gets replaced with the
# WHERE-clause fragment built by ``_build_filter`` below; psycopg parameters
# (``%(param)s``) are passed via the ``params`` dict.
# ---------------------------------------------------------------------------

# Pass 1: same ``tool_use_id`` appearing more than once in a single message's
# blocks array. ``WITH ORDINALITY`` lets us also surface the array positions
# of the dupes so the operator can pinpoint which entries collided.
SQL_INTRA_MESSAGE_DUPLICATES = """
WITH tool_use_blocks AS (
    SELECT
        m.id          AS message_id,
        m.session_id,
        m.created_at,
        b.value       AS block,
        b.ordinality  AS block_index
    FROM message m,
         jsonb_array_elements(m.blocks) WITH ORDINALITY AS b(value, ordinality)
    WHERE m.role = 'ASSISTANT'
      AND b.value->>'type' = 'tool_use'
      {filter_sql}
)
SELECT
    message_id,
    session_id::text,
    created_at,
    block->>'tool'        AS tool,
    block->>'tool_use_id' AS tool_use_id,
    COUNT(*)              AS occurrences,
    ARRAY_AGG(block_index ORDER BY block_index) AS array_indices
FROM tool_use_blocks
GROUP BY message_id, session_id, created_at,
         block->>'tool', block->>'tool_use_id'
HAVING COUNT(*) > 1
ORDER BY occurrences DESC, created_at DESC
LIMIT %(limit)s
"""

# Pass 2: same ``tool_use_id`` appearing across multiple messages of one
# session. We dedupe within each message first (so a Pass-1 hit doesn't
# also show up here) then group by (session_id, tool_use_id).
SQL_CROSS_MESSAGE_DUPLICATES = """
WITH tool_use_blocks AS (
    SELECT DISTINCT
        m.id          AS message_id,
        m.session_id,
        m.created_at,
        b.value->>'tool_use_id' AS tool_use_id
    FROM message m,
         jsonb_array_elements(m.blocks) AS b
    WHERE m.role = 'ASSISTANT'
      AND b.value->>'type' = 'tool_use'
      {filter_sql}
)
SELECT
    session_id::text,
    tool_use_id,
    COUNT(DISTINCT message_id) AS messages_with_id,
    ARRAY_AGG(DISTINCT message_id ORDER BY message_id) AS message_ids,
    MIN(created_at) AS first_seen,
    MAX(created_at) AS last_seen
FROM tool_use_blocks
GROUP BY session_id, tool_use_id
HAVING COUNT(DISTINCT message_id) > 1
ORDER BY messages_with_id DESC, first_seen DESC
LIMIT %(limit)s
"""

# Pass 3: full blocks of a specific message — used for ``--message-id``.
SQL_MESSAGE_DETAIL = """
SELECT id, session_id::text, created_at, blocks
FROM message
WHERE id = %(message_id)s
"""

# Aggregate: how many assistant messages exist in the lookback, so the
# operator can put the duplicate count in proportion (e.g. "3 dupes
# across 50k messages" vs "3 across 80").
SQL_TOTAL_ASSISTANT_MESSAGES = """
SELECT COUNT(*)
FROM message
WHERE role = 'ASSISTANT' {filter_sql}
"""


def _build_filter(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Build the WHERE fragment + params for the chosen filters.

    The fragment is concatenated into the SQL with ``str.format`` —
    safe because every branch uses a parameterised placeholder, never
    inlining user input. ``session_id`` is validated as a uuid string;
    ``days`` is an int by argparse type.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": args.limit}
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


def _run_intra_message(cur: psycopg.Cursor, args: argparse.Namespace) -> int:
    filter_sql, params = _build_filter(args)
    cur.execute(SQL_INTRA_MESSAGE_DUPLICATES.format(filter_sql=filter_sql), params)
    rows = cur.fetchall()
    _print_header(
        f"PASS 1 — duplicate tool_use_id within a single message  (top {args.limit})"
    )
    if not rows:
        print("  No intra-message duplicates found in the chosen window. ✓")
        return 0
    print(
        f"  {'message_id':>11}  {'tool':<14}  {'tool_use_id':<32}  "
        f"{'occ':>3}  array_idx                created_at         session_id"
    )
    for (
        message_id, session_id, created_at, tool, tool_use_id, occ, idx,
    ) in rows:
        idx_str = ",".join(str(i) for i in idx)
        print(
            f"  {message_id:>11}  {tool:<14}  {tool_use_id:<32}  "
            f"{occ:>3}  [{idx_str:<22}] {created_at:%Y-%m-%d %H:%M:%S}  {session_id}"
        )
    print(
        f"\n  → {len(rows)} message(s) carry duplicate tool_use blocks. "
        f"Pick one and re-run with --message-id <id> to see the raw blocks."
    )
    return len(rows)


def _run_cross_message(cur: psycopg.Cursor, args: argparse.Namespace) -> int:
    filter_sql, params = _build_filter(args)
    cur.execute(SQL_CROSS_MESSAGE_DUPLICATES.format(filter_sql=filter_sql), params)
    rows = cur.fetchall()
    _print_header(
        f"PASS 2 — same tool_use_id across multiple messages of one session  "
        f"(top {args.limit})"
    )
    if not rows:
        print("  No cross-message duplicates found in the chosen window. ✓")
        return 0
    print(
        f"  {'session_id':<38}  {'tool_use_id':<32}  msgs  "
        f"first_seen           last_seen"
    )
    for (
        session_id, tool_use_id, messages_with_id, message_ids, first_seen,
        last_seen,
    ) in rows:
        ids = ",".join(str(i) for i in message_ids)
        print(
            f"  {session_id:<38}  {tool_use_id:<32}  {messages_with_id:>4}  "
            f"{first_seen:%Y-%m-%d %H:%M:%S}  {last_seen:%Y-%m-%d %H:%M:%S}"
        )
        print(f"    └ message ids: {ids}")
    print(
        f"\n  → {len(rows)} (session, tool_use_id) pair(s) span multiple "
        f"messages. Strongly suggests lease-race re-persistence."
    )
    return len(rows)


def _run_message_detail(cur: psycopg.Cursor, message_id: int) -> int:
    cur.execute(SQL_MESSAGE_DETAIL, {"message_id": message_id})
    row = cur.fetchone()
    _print_header(f"MESSAGE DETAIL — id={message_id}")
    if row is None:
        print(f"  No message with id={message_id}.")
        return 1
    msg_id, session_id, created_at, blocks = row
    print(f"  message_id  = {msg_id}")
    print(f"  session_id  = {session_id}")
    print(f"  created_at  = {created_at}")
    print(f"  block_count = {len(blocks)}")
    # Group by tool_use_id so byte-identical-vs-different is obvious.
    by_id: dict[str, list[tuple[int, dict]]] = {}
    for i, block in enumerate(blocks):
        if block.get("type") != "tool_use":
            continue
        by_id.setdefault(block.get("tool_use_id", "<missing>"), []).append((i, block))
    dupes = {tid: items for tid, items in by_id.items() if len(items) > 1}
    if not dupes:
        print("\n  This message has no duplicate tool_use_id. ✓")
        return 0
    for tid, items in dupes.items():
        print(f"\n  ── duplicate tool_use_id={tid!r} ({len(items)} blocks) ──")
        for arr_idx, block in items:
            print(f"\n    [block #{arr_idx}]")
            print("    " + json.dumps(block, indent=2, ensure_ascii=False).replace(
                "\n", "\n    "
            ))
    return len(dupes)


def _print_context(cur: psycopg.Cursor, args: argparse.Namespace) -> None:
    filter_sql, params = _build_filter(args)
    # ``filter_sql`` references ``m.created_at`` — strip the alias for
    # this query since it has no JOIN.
    bare_filter = filter_sql.replace("m.session_id", "session_id").replace(
        "m.created_at", "created_at"
    )
    cur.execute(
        SQL_TOTAL_ASSISTANT_MESSAGES.format(filter_sql=bare_filter), params,
    )
    (total,) = cur.fetchone()
    print(f"  Assistant messages in window: {total:,}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose duplicate tool_use_id in message.blocks.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--session-id",
        help="UUID of one session to scope the check to. Defaults to all sessions.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Lookback window in days. Set to 0 to scan all history.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max rows to print per pass.",
    )
    parser.add_argument(
        "--message-id",
        type=int,
        help="If set, skip the aggregate passes and dump the raw blocks "
        "of this message id grouped by tool_use_id.",
    )
    args = parser.parse_args()
    if args.days == 0:
        args.days = None  # disables the date filter

    psycopg_uri = settings.db_uri.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(psycopg_uri) as conn, conn.cursor() as cur:
        if args.message_id is not None:
            return 0 if _run_message_detail(cur, args.message_id) == 0 else 1

        scope = args.session_id or "(all sessions)"
        window = f"last {args.days} days" if args.days else "all history"
        _print_header(f"SCOPE: {scope}    WINDOW: {window}")
        _print_context(cur, args)

        intra = _run_intra_message(cur, args)
        cross = _run_cross_message(cur, args)

        _print_header("VERDICT")
        if intra == 0 and cross == 0:
            print(
                "  DB is clean. The duplicate React key warning then most "
                "likely comes from the live SSE path: backend re-streams\n"
                "  events on EventSource reconnect and the frontend reducer "
                "isn't idempotent against replay. Check that\n"
                "  ``Last-Event-ID`` is forwarded by both nginx and the "
                "Next.js proxy /api/sessions/[id]/stream route."
            )
            return 0
        if intra > 0 and cross == 0:
            print(
                "  Duplicates are confined to within-message blocks. Most "
                "likely axon's _finalize_tool_calls let two streamed\n"
                "  tool_call deltas resolve to the same tool_call.id "
                "(Kimi K2's functions.<name>:<seq> counter occasionally\n"
                "  repeats inside one response). Both axon and "
                "agent_worker.domain.blocks.build_blocks should dedupe by id."
            )
            return 1
        if cross > 0:
            print(
                "  The same tool_use_id appears across multiple messages of "
                "one session. Strongly suggests the worker re-persisted\n"
                "  the same turn (lease race after a stale-lease reclaim, or "
                "the recovery cron sweeping a turn that's still alive).\n"
                "  Inspect TurnRepository / lease semantics in agent_worker."
            )
            return 2
        return 0


if __name__ == "__main__":
    sys.exit(main())
