"""Upload a skill bundle (.zip) as a BUILTIN skill.

Reads a local .zip file, validates it (same checks as the user-facing
``POST /api/skills`` endpoint), uploads to the skill OSS bucket, and
inserts a ``skill`` row with ``origin='BUILTIN', user_id=NULL``.

Idempotent by name: if a BUILTIN skill with the same name already
exists, the row is updated (new bundle_key + description) rather than
duplicated.

Usage:
    cd backend
    uv run python -m scripts.upload_skill path/to/my-skill.zip
    uv run python -m scripts.upload_skill path/to/another.zip --disabled
"""

import argparse
import re
import sys
import uuid
from pathlib import Path

import psycopg

from app.services import file_storage
from app.services.file_storage import SKILL_POLICY
from app.services.skill_bundle import (
    InvalidSkillBundleError,
    normalize_bundle,
    parse_bundle,
)
from config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a .zip as a BUILTIN skill.")
    parser.add_argument("zip_path", type=Path, help="Path to the skill bundle .zip")
    parser.add_argument(
        "--disabled", action="store_true",
        help="Insert with enabled=false (skill exists in DB but not offered to users).",
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
    print(f"  description: {parsed.description}")

    file_key = f"builtin/{parsed.name}/{uuid.uuid4()}/{zip_path.name}"
    file_storage.upload(SKILL_POLICY, file_key, data)
    print(f"  uploaded to OSS: {file_key}")

    # Upsert DB row. psycopg uses the sync ``postgresql://`` URL, so
    # strip the SQLAlchemy ``+asyncpg`` driver suffix that the app's
    # async session uses.
    enabled = not args.disabled
    db_uri = re.sub(r"postgresql\+\w+", "postgresql", settings.db_uri)
    with psycopg.connect(db_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO skill (user_id, origin, name, display_name, description,
                                   bundle_key, bundle_size, enabled)
                VALUES (NULL, 'BUILTIN', %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) WHERE user_id IS NULL
                DO UPDATE SET description = EXCLUDED.description,
                              display_name = EXCLUDED.display_name,
                              bundle_key = EXCLUDED.bundle_key,
                              bundle_size = EXCLUDED.bundle_size,
                              enabled = EXCLUDED.enabled,
                              updated_at = now()
                """,
                (parsed.name, parsed.name, parsed.description,
                 file_key, len(data), enabled),
            )
        conn.commit()

    status = "enabled" if enabled else "disabled"
    print(f"  DB upserted (BUILTIN, {status})")
    print("Done.")


if __name__ == "__main__":
    main()
