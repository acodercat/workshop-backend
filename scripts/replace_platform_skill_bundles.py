"""Repoint platform skills to freshly-packaged bundles from a local skills dir.

For each named skill, zip its directory (``SKILL.md`` at the zip root), run it
through the SAME structural-validation + normalization path the upload endpoint
uses (``validate_zip_structure`` -> ``normalize_bundle`` -> ``parse_bundle``),
so the stored bytes are exactly what a real upload would store. Then upload to
the skill OSS bucket and repoint the platform ``skill`` row's ``bundle_key`` /
``bundle_size`` / ``updated_at`` — curated metadata (name / display_name /
display_name_i18n / description / origin / enabled) is left untouched — and
finally delete the previous OSS object.

``bundle_size`` is updated alongside ``bundle_key`` because it is a physical
property of the bundle file (leaving it stale would misreport the new bundle),
not curated metadata.

Only touches PLATFORM skills (``user_id IS NULL``) whose name matches a
``<name>/`` subdir under ``--src``. Dry-run by default; pass ``--apply`` to
write. Prints old -> new keys for rollback.

    cd backend
    uv run python -m scripts.replace_platform_skill_bundles --src ../agent-skills/anthropic-skills/skills
    uv run python -m scripts.replace_platform_skill_bundles --src ../agent-skills/anthropic-skills/skills --apply
"""

import argparse
import io
import re
import uuid
import zipfile
from pathlib import Path

import psycopg

from app.services import file_storage
from app.services.file_storage import SKILL_POLICY
from app.services.skill_bundle import (
    InvalidSkillBundleError,
    normalize_bundle,
    parse_bundle,
    validate_zip_structure,
)
from config import settings

# Platform skills to repoint. Each must exist both as a ``<name>/`` subdir
# under ``--src`` and as a ``user_id IS NULL`` row in the ``skill`` table.
SKILLS = ["docx", "pdf", "pptx", "xlsx", "frontend-design"]

# Build junk that must never make it into a bundle.
_SKIP_NAMES = {"__pycache__", ".DS_Store"}


def _zip_skill_dir(skill_dir: Path) -> bytes:
    """Zip a skill directory with ``SKILL.md`` at the archive root."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(skill_dir)
            if any(part in _SKIP_NAMES for part in rel.parts):
                continue
            if rel.suffix == ".pyc":
                continue
            zf.write(path, rel.as_posix())
    return buf.getvalue()


def _validate(raw: bytes) -> tuple[bytes, str]:
    """Mirror the upload endpoint's ``_validate_and_parse`` for a zip:
    structure check, then normalize, then parse. Returns the stored bytes
    and the SKILL.md name."""
    validate_zip_structure(raw)
    normalized = normalize_bundle(raw)
    data = normalized if normalized is not None else raw
    return data, parse_bundle(data).name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        type=Path,
        required=True,
        help="Directory containing <skill-name>/ subdirs (each with SKILL.md).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes (default: dry-run preview only).",
    )
    args = parser.parse_args()

    # psycopg wants the sync ``postgresql://`` URL; strip the SQLAlchemy
    # ``+asyncpg``/``+psycopg`` driver suffix the app config carries.
    db_uri = re.sub(r"postgresql\+\w+", "postgresql", settings.db_uri)

    done = 0
    with psycopg.connect(db_uri) as conn:
        for name in SKILLS:
            skill_dir = args.src / name
            if not (skill_dir / "SKILL.md").is_file():
                print(f"  ✗ {name}: no SKILL.md under {skill_dir}")
                continue

            raw = _zip_skill_dir(skill_dir)
            try:
                data, parsed_name = _validate(raw)
            except InvalidSkillBundleError as exc:
                print(f"  ✗ {name}: invalid bundle — {exc}")
                continue
            if parsed_name != name:
                print(
                    f"  ✗ {name}: SKILL.md name is {parsed_name!r}, "
                    f"expected {name!r} — skipped"
                )
                continue

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT origin, bundle_key FROM skill "
                    "WHERE name = %s AND user_id IS NULL",
                    (name,),
                )
                row = cur.fetchone()
            if row is None:
                print(f"  ✗ {name}: no platform skill row (user_id IS NULL)")
                continue
            origin, old_key = row
            new_key = f"{origin.lower()}/{name}/{uuid.uuid4()}/{name}.zip"

            print(f"  {name}: {len(data)} bytes, name ok")
            print(f"     old: {old_key}")
            print(f"     new: {new_key}")

            if args.apply:
                # Order: upload new -> repoint (commit) -> delete old. A crash
                # before commit leaves the row on the old (still-present) key;
                # the delete runs only after the repoint is durable.
                file_storage.upload(SKILL_POLICY, new_key, data)
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE skill "
                        "SET bundle_key = %s, bundle_size = %s, updated_at = now() "
                        "WHERE name = %s AND user_id IS NULL",
                        (new_key, len(data), name),
                    )
                conn.commit()
                file_storage.delete(SKILL_POLICY, old_key)  # best-effort cleanup
                print("     ✅ uploaded + repointed + old bundle deleted")
            done += 1

    if args.apply:
        print(f"\n✅ repointed {done} skill(s)")
    else:
        print(f"\n(dry-run) {done} skill(s) would change — pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
