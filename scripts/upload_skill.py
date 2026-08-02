"""Upload a skill bundle (.zip) as a platform skill (SYSTEM/OFFICIAL).

Reads a local .zip file, validates it (same checks as the user-facing
``POST /api/skills`` endpoint), uploads to the skill OSS bucket, and
inserts a platform ``skill`` row (``user_id=NULL``) with the chosen
``origin`` (``SYSTEM`` by default, ``--origin OFFICIAL`` for opt-in
skills).

``enabled`` is derived from the origin. SYSTEM skills land enabled —
they are agent-only and reach the worker through the same effective-
enabled read as everything else, so disabled means invisible to the
agent (``--disabled`` exists for staging a bundle without exposing
it). OFFICIAL skills always land **disabled**: ``skill.enabled`` is
the default every user who never touched the toggle falls back to
(``COALESCE(pref.enabled, skill.enabled)``), and an enabled-by-default
official skill would push its metadata into every user's every-turn
LLM context. Users opt in from the skills page — or just pick the
skill in the composer, which doesn't consult the toggle at all.

Idempotent by name: if a platform skill with the same name already
exists, the row is updated (new bundle_key + description + origin)
rather than duplicated.

Usage:
    cd backend
    uv run python -m scripts.upload_skill path/to/my-skill.zip
    uv run python -m scripts.upload_skill path/to/another.zip --origin OFFICIAL
    uv run python -m scripts.upload_skill path/to/another.zip --disabled
"""

import argparse
import re
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from app.services import file_storage
from app.services.file_storage import SKILL_POLICY
from app.services.skill_bundle import (
    InvalidSkillBundleError,
    normalize_bundle,
    parse_bundle,
)
from config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a .zip as a platform skill.")
    parser.add_argument("zip_path", type=Path, help="Path to the skill bundle .zip")
    parser.add_argument(
        "--origin", choices=("SYSTEM", "OFFICIAL"), default="SYSTEM",
        help="Platform origin: SYSTEM (always-on core, default) or "
             "OFFICIAL (user opts in).",
    )
    parser.add_argument(
        "--disabled", action="store_true",
        help="SYSTEM only: insert with enabled=false (bundle exists in DB "
             "but stays invisible to the agent). OFFICIAL is always "
             "disabled — the toggle default is per-user opt-in.",
    )
    args = parser.parse_args()

    zip_path: Path = args.zip_path
    if not zip_path.exists():
        sys.exit(f"File not found: {zip_path}")
    if zip_path.suffix.lower() != ".zip":
        sys.exit(f"Expected a .zip file, got: {zip_path.name}")

    data = zip_path.read_bytes()
    # Normalize wrapper-dir zips so the bundle in OSS has ``SKILL.md``
    # at root — same step ``register_skill`` does for user uploads.
    # Without this the worker fails to find SKILL.md after unzip.
    normalized = normalize_bundle(data)
    if normalized is not None:
        data = normalized
    print(f"Read {len(data)} bytes from {zip_path}")

    try:
        parsed = parse_bundle(data)
    except InvalidSkillBundleError as e:
        sys.exit(f"Invalid skill bundle: {e}")

    print(f"  name: {parsed.name}")
    print(f"  display_name: {parsed.display_name}")
    if parsed.display_name_i18n:
        print(f"  display_name_i18n: {parsed.display_name_i18n}")
    print(f"  description: {parsed.description}")

    file_key = f"{args.origin.lower()}/{parsed.name}/{uuid.uuid4()}/{zip_path.name}"
    file_storage.upload(SKILL_POLICY, file_key, data)
    print(f"  uploaded to OSS: {file_key}")

    # Upsert DB row. psycopg uses the sync ``postgresql://`` URL, so
    # strip the SQLAlchemy ``+asyncpg`` driver suffix that the app's
    # async session uses.
    enabled = args.origin == "SYSTEM" and not args.disabled
    db_uri = re.sub(r"postgresql\+\w+", "postgresql", settings.db_uri)
    with psycopg.connect(db_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO skill (user_id, origin, name, display_name,
                                   display_name_i18n, description,
                                   bundle_key, bundle_size, enabled)
                VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) WHERE user_id IS NULL
                DO UPDATE SET origin = EXCLUDED.origin,
                              description = EXCLUDED.description,
                              display_name = EXCLUDED.display_name,
                              display_name_i18n = EXCLUDED.display_name_i18n,
                              bundle_key = EXCLUDED.bundle_key,
                              bundle_size = EXCLUDED.bundle_size,
                              enabled = EXCLUDED.enabled,
                              updated_at = now()
                """,
                (args.origin, parsed.name, parsed.display_name,
                 Jsonb(parsed.display_name_i18n) if parsed.display_name_i18n else None,
                 parsed.description, file_key, len(data), enabled),
            )
        conn.commit()

    status = "enabled" if enabled else "disabled"
    print(f"  DB upserted ({args.origin}, {status})")
    print("Done.")


if __name__ == "__main__":
    main()
