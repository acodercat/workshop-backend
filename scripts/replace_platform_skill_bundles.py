"""Repoint platform skills to freshly-packaged bundles from a local skills dir.

For each named skill, zip its directory (``SKILL.md`` at the zip root) and
ship it through the registration service
(:func:`app.services.skill_registration.register_platform_skill_bundle`), so
the stored bytes, the key and the row are exactly what a console upload
writes. The listing an operator curated is kept: ``display_name`` /
``display_name_i18n`` / ``description`` come from the existing row, not the
bundle, and ``origin`` / ``status`` / ``category_id`` stay where they are.

The previous bundle object is left in OSS, as the service leaves it — a turn
may be staging it — and its key is printed, so an unwanted version can be
pointed back to.

Only touches PLATFORM skills (``user_id IS NULL``) whose name matches a
``<name>/`` subdir under ``--src``. Dry-run by default — it validates each
bundle and writes nothing; pass ``--apply`` to write.

    cd backend
    uv run python -m scripts.replace_platform_skill_bundles --src ../agent-skills/anthropic-skills/skills
    uv run python -m scripts.replace_platform_skill_bundles --src ../agent-skills/anthropic-skills/skills --apply
"""

import argparse
import asyncio
import io
import zipfile
from pathlib import Path

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.errors import AppError
from app.models.skill import Skill
from app.services.skill_registration import parse_payload, register_platform_skill_bundle

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


async def repoint(src: Path, *, apply: bool) -> int:
    """Repoint each of :data:`SKILLS` found under *src*; returns how many
    were (or, on a dry run, would be) repointed. A bundle the service
    refuses (an ``AppError``) is reported and skipped, so one bad bundle does
    not stop the rest; any other failure ends the run, the skills before it
    already committed."""
    done = 0
    async with AsyncSessionLocal() as db:
        for name in SKILLS:
            skill_dir = src / name
            if not (skill_dir / "SKILL.md").is_file():
                print(f"  ✗ {name}: no SKILL.md under {skill_dir}")
                continue

            filename = f"{name}.zip"
            raw = _zip_skill_dir(skill_dir)
            try:
                data, parsed, _ = await parse_payload(filename, raw)
            except AppError as exc:
                print(f"  ✗ {name}: invalid bundle — {exc}")
                continue
            # The service files a bundle under the name in its SKILL.md, so
            # a mismatch would replace (or create) some other skill.
            if parsed.name != name:
                print(f"  ✗ {name}: SKILL.md name is {parsed.name!r}, expected {name!r} — skipped")
                continue

            skill = await db.scalar(
                select(Skill).where(Skill.user_id.is_(None), Skill.name == name)
            )
            if skill is None:
                print(f"  ✗ {name}: no platform skill row (user_id IS NULL)")
                continue

            print(f"  {name}: {len(data)} bytes, name ok")
            print(f"     old: {skill.bundle_key}")

            if apply:
                try:
                    skill, _ = await register_platform_skill_bundle(
                        db, origin=skill.origin, filename=filename, data=raw, keep_listing=True
                    )
                except AppError as exc:
                    await db.rollback()
                    print(f"  ✗ {name}: rejected — {exc}")
                    continue
                await db.commit()
                print(f"     new: {skill.bundle_key}")
                print("     ✅ uploaded + repointed (old bundle kept)")
            done += 1
    return done


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

    done = asyncio.run(repoint(args.src, apply=args.apply))
    if args.apply:
        print(f"\n✅ repointed {done} skill(s)")
    else:
        print(f"\n(dry-run) {done} skill(s) would change — pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
