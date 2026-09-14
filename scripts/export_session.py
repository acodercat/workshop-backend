"""Export one session (row + messages + all files) into a portable bundle for
import on another deployment (e.g. the internal-network mep).

Read-only on the source DB; downloads every ``session_files`` object from OSS.
The companion importer is ``backend-mep/scripts/import_session.py``, which
uploads the files to the target object store under their original keys and
inserts the session re-owned by a target user.

Run from ``backend/`` (this deployment's OSS + source DB config):

    uv run python scripts/export_session.py --session-id <uuid>
    uv run python scripts/export_session.py --session-id <uuid> --out /path/to/bundle

Produces ``<out>/manifest.json`` + ``<out>/files/NNNN.bin``. The bundle is
self-contained — transfer the whole ``<out>`` directory to the target.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import psycopg

from app.services import file_storage
from config import settings


def main() -> None:
    ap = argparse.ArgumentParser(description="Export a session into a portable bundle.")
    ap.add_argument("--session-id", required=True, help="Source session UUID.")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("session_migration"),
        help="Output bundle directory (default: ./session_migration).",
    )
    args = ap.parse_args()
    sid, out = args.session_id, args.out
    files_dir = out / "files"

    # Fresh bundle: clear a prior export so a re-run for a different session
    # can't leave stale higher-index files behind.
    if files_dir.exists():
        shutil.rmtree(files_dir)
    files_dir.mkdir(parents=True, exist_ok=True)

    uri = re.sub(r"postgresql\+\w+", "postgresql", settings.db_uri)
    with psycopg.connect(uri) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT title, model_id, tier, agent_type, thinking_enabled, status, "
            "created_at, updated_at FROM session WHERE id=%s",
            (sid,),
        )
        row = cur.fetchone()
        if row is None:
            sys.exit(f"source session {sid} not found")
        title, model_id, tier, agent_type, thinking, status, created, updated = row
        session = {
            "title": title,
            "model_id": model_id,
            "tier": tier,
            "agent_type": agent_type,
            "thinking_enabled": thinking,
            "status": status,
            "created_at": created.isoformat(),
            "updated_at": updated.isoformat(),
        }

        cur.execute(
            "SELECT role, blocks, created_at FROM message WHERE session_id=%s ORDER BY id",
            (sid,),
        )
        messages = [
            {"role": r, "blocks": b, "created_at": c.isoformat()} for r, b, c in cur.fetchall()
        ]

        cur.execute(
            "SELECT file_key, origin, filename, mime_type, size_bytes, created_at "
            "FROM session_files WHERE session_id=%s ORDER BY id",
            (sid,),
        )
        file_rows = cur.fetchall()

    # An absolute OSS URL baked into a block would not survive the move.
    blob = json.dumps(messages, ensure_ascii=False)
    if "aliyuncs" in blob or re.search(r'https?://[^"]*oss', blob):
        print("  ⚠️ blocks contain absolute OSS URLs — inspect before importing")

    files = []
    for idx, (key, origin, fn, mt, sz, cts) in enumerate(file_rows, start=1):
        data = file_storage.download(file_storage.SESSION_FILE_POLICY, key)
        local = f"files/{idx:04d}.bin"
        (out / local).write_bytes(data)
        warn = "" if sz in (None, len(data)) else f"  ⚠️ size db={sz} got={len(data)}"
        print(f"  [{origin:<11}] {len(data):>9}  {fn}{warn}")
        files.append(
            {
                "file_key": key,
                "origin": origin,
                "filename": fn,
                "mime_type": mt,
                "size_bytes": len(data),
                "created_at": cts.isoformat(),
                "local": local,
            }
        )

    manifest = {"source_session_id": sid, "session": session, "messages": messages, "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    total = sum(f["size_bytes"] for f in files)
    print(
        f"\nexported: {len(messages)} messages, {len(files)} files, "
        f"{total / 1024 / 1024:.1f} MB -> {out}"
    )


if __name__ == "__main__":
    main()
