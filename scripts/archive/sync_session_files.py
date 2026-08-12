"""Backfill ``session_files`` from ``Message.blocks``.

Reconciles the file→session ownership table against the historical
JSON record on ``message.blocks``. Idempotent — every INSERT is gated
by ``ON CONFLICT (file_key) DO NOTHING``, so re-running is a no-op
once the table is in sync.

The same SQL ran inline in alembic ``f681f1d56d1c_add_session_files_table``
when the table was first created. This script exists for the cases that
migration can't cover:

- Data restored from a backup taken before the migration ran.
- Rows imported from a different environment (export/import bypasses
  the application code path that writes ``session_files``).
- Reconciliation after an incident where the worker was unable to
  insert ``session_files`` for some tool outputs.

The script does not delete drift in the other direction — rows in
``session_files`` whose ``file_key`` does not appear in any message —
because tool-output ``file_key``s are written to ``session_files``
*before* the assistant message is persisted, so a transient view in
the middle of a turn legitimately has rows here that aren't yet in
``message.blocks``. A drift-cleaner would need to know which keys are
"old enough to be orphaned"; out of scope for a sync.

Usage:
    uv run python -m scripts.archive.sync_session_files
"""

import psycopg

from config import settings

# Walk every message's JSONB ``blocks`` column, flatten nested ``files``
# arrays, INSERT one row per (session_id, file_key). LATERAL ties the
# unnesting to each message row so the join doesn't explode; COALESCE
# tolerates blocks without a ``files`` field; the ``f ? 'file_key'``
# check skips malformed entries.
SQL_BACKFILL = """
INSERT INTO session_files (session_id, file_key, created_at)
SELECT m.session_id, f->>'file_key', m.created_at
FROM message m
     CROSS JOIN LATERAL jsonb_array_elements(m.blocks) AS b
     CROSS JOIN LATERAL
         jsonb_array_elements(COALESCE(b->'files', '[]'::jsonb)) AS f
WHERE f ? 'file_key'
ON CONFLICT (file_key) DO NOTHING
"""

# Distinct ``file_key``s referenced by any message — the upper bound
# for what ``session_files`` should contain *from this source*. Tool
# outputs written by the worker but whose terminal message hasn't
# committed yet are not in messages, so the live table can legitimately
# exceed this count.
SQL_DISTINCT_REFERENCED = """
SELECT count(DISTINCT f->>'file_key')
FROM message m
     CROSS JOIN LATERAL jsonb_array_elements(m.blocks) AS b
     CROSS JOIN LATERAL
         jsonb_array_elements(COALESCE(b->'files', '[]'::jsonb)) AS f
WHERE f ? 'file_key'
"""

SQL_COUNT_TABLE = "SELECT count(*) FROM session_files"


def main() -> None:
    psycopg_uri = settings.db_uri.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(psycopg_uri) as conn, conn.cursor() as cur:
        cur.execute(SQL_COUNT_TABLE)
        before = cur.fetchone()[0]

        cur.execute(SQL_DISTINCT_REFERENCED)
        distinct_referenced = cur.fetchone()[0]

        cur.execute(SQL_BACKFILL)
        inserted = cur.rowcount

        cur.execute(SQL_COUNT_TABLE)
        after = cur.fetchone()[0]

        conn.commit()

    print(f"Distinct file_keys referenced in messages: {distinct_referenced}")
    print(f"session_files rows before:                 {before}")
    print(f"Newly inserted:                            {inserted}")
    print(f"session_files rows after:                  {after}")


if __name__ == "__main__":
    main()
