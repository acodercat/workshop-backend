"""One-time backfill of ``display_name_i18n`` for the existing OFFICIAL skills.

OFFICIAL skills seeded before the ``metadata.display_name`` map existed carry
only a slug as their display name; this backfills the canonical zh-CN / en-US
names so the skill picker shows them localized.

SYSTEM skills are intentionally excluded — they are agent-only and never
surface in the user-facing list, so a localized name would be dead data.

Going forward, re-uploading a bundle whose SKILL.md ships a
``metadata.display_name`` map is the canonical path — this script is not part
of it, and adding a NEW skill's names here is the wrong place (ship them in
the bundle instead).

Reuses :func:`app.services.localized_text.parse`, so the stored
``(display_name, display_name_i18n)`` pair matches exactly what an upload
would produce. Idempotent: re-running sets the same values.

Dry-run by default (prints what would change); pass ``--apply`` to write.

    uv run python -m scripts.archive.backfill_official_skill_i18n           # preview
    uv run python -m scripts.archive.backfill_official_skill_i18n --apply   # commit
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.skill import Skill, SkillOrigin
from app.services import localized_text

# Canonical zh-CN / en-US names for the seeded OFFICIAL catalog, keyed by the
# skill's ``name`` (slug). Adding a NEW skill does not belong here — ship its
# names in the bundle's ``metadata.display_name`` instead.
OFFICIAL_SKILL_NAMES: dict[str, dict[str, str]] = {
    "video-creation": {"zh-CN": "视频创作", "en-US": "Video Creation"},
    "fireworks-tech-graph": {"zh-CN": "技术图示", "en-US": "Technical Diagram"},
    "canvas-design": {"zh-CN": "画布设计", "en-US": "Canvas Design"},
    "nature-figure": {"zh-CN": "Nature科研配图", "en-US": "Nature Figure"},
}


async def _backfill(db: AsyncSession, *, apply: bool) -> None:
    present = set(
        (await db.execute(select(Skill.name).where(Skill.origin == SkillOrigin.OFFICIAL)))
        .scalars()
        .all()
    )

    updated = 0
    for slug, names in OFFICIAL_SKILL_NAMES.items():
        if slug not in present:
            print(f"  skip  {slug!r} — no OFFICIAL skill with this name")
            continue
        # 128: the ``skill.display_name`` column width.
        display_name, display_name_i18n = localized_text.parse(names, slug, max_chars=128)
        print(f"  set   {slug!r} → default {display_name!r}, i18n {display_name_i18n}")
        if apply:
            await db.execute(
                update(Skill)
                .where(Skill.name == slug, Skill.origin == SkillOrigin.OFFICIAL)
                .values(
                    display_name=display_name,
                    display_name_i18n=display_name_i18n,
                )
            )
        updated += 1

    if apply:
        await db.commit()
        print(f"\n✅ committed {updated} skill(s)")
    else:
        print(f"\n(dry-run) {updated} skill(s) would change — pass --apply to write")


async def main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        await _backfill(db, apply=args.apply)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes (default: dry-run preview only).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(_parse_args())))
