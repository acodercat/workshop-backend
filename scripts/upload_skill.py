"""Upload a skill bundle as a platform skill (SYSTEM/OFFICIAL).

The console does this too (``POST /skills`` on the admin surface); both
call :func:`app.services.skill_registration.register_platform_skill_bundle`,
so the validation, the OSS key scheme and the replace-by-name behaviour
are one implementation with two front doors. This script is the path
that still works when the console is down, or when all you have is SSH.

A new listing lands ``STAGED``: uploaded, validated, reaching nobody.
Uploading is not publishing, and the one thing an upload cannot undo is
having been visible. ``--publish`` collapses the two steps for the case
where an operator already knows; otherwise the listing goes live from
the console.

Whether a user then *wants* an ``OFFICIAL`` skill is a separate question
on a separate row: they opt in from the skills page, or pick the skill
in the composer, which consults the listing's status but not their
toggle.

Idempotent by name: re-uploading a name that exists replaces that
skill's bundle in place, keeping its id and every user's preference.
Nothing about policy moves with it — not the listing's status either —
because shipping a fix is a content change.

``--category`` files the skill under one of the tabs the skills page
groups by, **resolved by name rather than by id**: a name is what the
operator reads off ``GET /skill-categories``, and a typo in one fails loudly here
while a mistyped id would quietly file the skill under the wrong tab.
Omitting it leaves the category alone, so re-uploading a bundle keeps
where it was filed. Only OFFICIAL skills are browsed by tab — nothing
rejects it on a SYSTEM upload, it is simply never read. Categories themselves are
created on the admin API (``POST /skill-categories``) — the console has
the routes but not yet a screen for them, so today that is a request you
make by hand. This script files a bundle under one; it does not create one.

Usage:
    cd backend
    uv run python -m scripts.upload_skill path/to/my-skill.zip
    uv run python -m scripts.upload_skill path/to/another.zip --origin OFFICIAL
    uv run python -m scripts.upload_skill path/to/another.zip --publish
    uv run python -m scripts.upload_skill path/to/another.zip \
        --origin OFFICIAL --category 写作
"""

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.errors import AppError
from app.models.skill import SkillOrigin
from app.models.skill_category import SkillCategory
from app.services.skill_registration import register_platform_skill_bundle


async def _resolve_category_id(db: AsyncSession, name: str) -> int:
    """Look a category up by the name the console shows, or exit.

    Listing the known names on failure is the whole point of resolving
    by name: a typo ends here with the answer beside it, rather than in
    a row filed under a tab nobody meant.
    """
    category_id = await db.scalar(select(SkillCategory.id).where(SkillCategory.name == name))
    if category_id is None:
        known = (await db.scalars(select(SkillCategory.name).order_by(SkillCategory.name))).all()
        sys.exit(
            f"No skill category named {name!r}. "
            f"Known: {', '.join(known) or '(none)'}\n"
            "Create one first: POST /skill-categories on the admin API."
        )
    return category_id


async def upload_bundle(
    bundle_path: Path, origin: SkillOrigin, publish: bool, category: str | None
) -> None:
    data = bundle_path.read_bytes()
    print(f"Read {len(data)} bytes from {bundle_path}")

    async with AsyncSessionLocal() as db:
        category_id = None if category is None else await _resolve_category_id(db, category)
        try:
            skill, replaced_key = await register_platform_skill_bundle(
                db,
                origin=origin,
                filename=bundle_path.name,
                data=data,
                # A new listing lands STAGED unless asked otherwise;
                # a replace never touches status at all.
                publish=publish,
                # ``None`` leaves the row's category alone — a re-upload
                # is a content change, not a refiling.
                category_id=category_id,
            )
        except AppError as exc:
            sys.exit(f"Rejected: {exc}")
        await db.commit()

        print(f"  name: {skill.name}")
        print(f"  display_name: {skill.display_name}")
        print(f"  description: {skill.description}")
        print(f"  uploaded to OSS: {skill.bundle_key}")
        if replaced_key:
            # Printed because the row no longer points at it: this is the
            # only way back to the previous version.
            print(f"  replaced bundle: {replaced_key}")
        if category is not None:
            print(f"  category: {category} (id={skill.category_id})")
        print(f"  DB upserted (id={skill.id}, {origin.value}, {skill.status.value})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a bundle as a platform skill.")
    parser.add_argument(
        "bundle_path",
        type=Path,
        help="Path to the skill bundle (.zip, .skill or .md)",
    )
    parser.add_argument(
        "--origin",
        choices=("SYSTEM", "OFFICIAL"),
        default="SYSTEM",
        help="Platform origin: SYSTEM (always-on core, default) or OFFICIAL (user opts in).",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Go live. A new listing is STAGED by default — uploaded and "
        "validated but reaching nobody — because the one thing an upload "
        "cannot undo is having been visible. Also promotes a listing that "
        "is already staged; without the flag a replace leaves status alone.",
    )
    parser.add_argument(
        "--category",
        help="File the skill under this category, by the name the console "
        "shows. Omit to leave an existing skill's category alone.",
    )
    args = parser.parse_args()

    bundle_path: Path = args.bundle_path
    if not bundle_path.exists():
        sys.exit(f"File not found: {bundle_path}")

    asyncio.run(upload_bundle(bundle_path, SkillOrigin(args.origin), args.publish, args.category))
    print("Done.")


if __name__ == "__main__":
    main()
