"""Upload a skill bundle as a platform skill (SYSTEM/OFFICIAL).

The console does this too (``POST /skills`` on the admin surface); both
call :func:`app.services.skill_registration.register_platform_skill_bundle`,
so the validation, the OSS key scheme and the replace-by-name behaviour
are one implementation with two front doors. This script is the path
that still works when the console is down, or when all you have is SSH.

``enabled`` follows the origin: SYSTEM skills land enabled — they are
agent-only and reach the worker through the same effective-enabled read
as everything else, so disabled means invisible to the agent
(``--disabled`` stages a bundle without exposing it). OFFICIAL skills
always land **disabled**: ``skill.enabled`` is the default every user
who never touched the toggle falls back to
(``COALESCE(pref.enabled, skill.enabled)``), and an enabled-by-default
official skill would push its metadata into every user's every-turn LLM
context. Users opt in from the skills page — or just pick the skill in
the composer, which doesn't consult the toggle at all.

Idempotent by name: re-uploading a name that exists replaces that
skill's bundle in place, keeping its id and every user's preference.

Usage:
    cd backend
    uv run python -m scripts.upload_skill path/to/my-skill.zip
    uv run python -m scripts.upload_skill path/to/another.zip --origin OFFICIAL
    uv run python -m scripts.upload_skill path/to/another.zip --disabled
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
    bundle_path: Path, origin: SkillOrigin, disabled: bool
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
                # ``None`` lets the origin decide (SYSTEM on, OFFICIAL
                # off); ``--disabled`` is the one case a caller overrides.
                enabled=False if disabled else None,
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
        status = "enabled" if skill.enabled else "disabled"
        print(f"  DB upserted (id={skill.id}, {origin.value}, {status})")


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
        "--disabled", action="store_true",
        help="SYSTEM only: insert with enabled=false (bundle exists in DB "
             "but stays invisible to the agent). OFFICIAL is always "
             "disabled — the toggle default is per-user opt-in.",
    )
    args = parser.parse_args()

    bundle_path: Path = args.bundle_path
    if not bundle_path.exists():
        sys.exit(f"File not found: {bundle_path}")

    asyncio.run(upload_bundle(bundle_path, SkillOrigin(args.origin), args.disabled))
    print("Done.")


if __name__ == "__main__":
    main()
