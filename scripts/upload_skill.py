"""Upload a skill bundle as a platform skill (SYSTEM/OFFICIAL).

The console does this too (``POST /skills`` on the admin surface); both
call :func:`app.services.skill_registration.register_platform_skill_bundle`,
so the validation, the OSS key scheme and the replace-by-name behaviour
are one implementation with two front doors. This script is the path
that still works when the console is down, or when all you have is SSH.

A new listing lands ``STAGED``: uploaded, validated, reaching nobody.
Uploading is not publishing, and the one thing an upload cannot undo is
having been visible. ``--publish`` says to go live — on a
new listing or one already staged.

Whether a user then *wants* an ``OFFICIAL`` skill is a separate question
on a separate row: they opt in from the skills page, or pick the skill
in the composer, which consults the listing's status but not their
toggle.

Idempotent by name: re-uploading a name that exists replaces that
skill's bundle in place, keeping its id and every user's preference.
Nothing about policy moves with it — not the listing's status either —
because shipping a fix is a content change.

Usage:
    cd backend
    uv run python -m scripts.upload_skill path/to/my-skill.zip
    uv run python -m scripts.upload_skill path/to/another.zip --origin OFFICIAL
    uv run python -m scripts.upload_skill path/to/another.zip --publish
"""

import argparse
import asyncio
import sys
from pathlib import Path

from app.database import AsyncSessionLocal
from app.errors import AppError
from app.models.skill import SkillOrigin
from app.services.skill_registration import register_platform_skill_bundle


async def upload_bundle(
    bundle_path: Path, origin: SkillOrigin, publish: bool
) -> None:
    data = bundle_path.read_bytes()
    print(f"Read {len(data)} bytes from {bundle_path}")

    async with AsyncSessionLocal() as db:
        try:
            skill, replaced_key = await register_platform_skill_bundle(
                db,
                origin=origin,
                filename=bundle_path.name,
                data=data,
                # A new listing lands STAGED unless asked otherwise;
                # a replace never touches status at all.
                publish=publish,
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
        print(
            f"  DB upserted (id={skill.id}, {origin.value}, "
            f"{skill.status.value})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload a bundle as a platform skill."
    )
    parser.add_argument(
        "bundle_path", type=Path,
        help="Path to the skill bundle (.zip, .skill or .md)",
    )
    parser.add_argument(
        "--origin", choices=("SYSTEM", "OFFICIAL"), default="SYSTEM",
        help="Platform origin: SYSTEM (always-on core, default) or "
             "OFFICIAL (user opts in).",
    )
    parser.add_argument(
        "--publish", action="store_true",
        help="Go live. A new listing is STAGED by default — uploaded and "
             "validated but reaching nobody. Also promotes a listing "
             "that is already staged; without the flag a replace leaves "
             "status alone.",
    )
    args = parser.parse_args()

    bundle_path: Path = args.bundle_path
    if not bundle_path.exists():
        sys.exit(f"File not found: {bundle_path}")

    asyncio.run(upload_bundle(bundle_path, SkillOrigin(args.origin), args.publish))
    print("Done.")


if __name__ == "__main__":
    main()
