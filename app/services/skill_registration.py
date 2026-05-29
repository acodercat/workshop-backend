"""Skill-bundle registration orchestration (the write path behind ``POST /skills``).

Validate the uploaded bytes, normalize wrapper zips, enforce the
per-user quota, upload to OSS, and INSERT the row — compensating the
OSS upload if the DB write fails. Pure byte-level validation lives in
:mod:`app.services.skill_bundle`; this module owns the DB + OSS side
effects so the endpoint stays a thin adapter.
"""

import asyncio
import logging
import os
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import (
    InvalidSkillBundleError,
    SkillBundleTooLargeError,
    SkillNameConflictError,
    SkillQuotaExceededError,
)
from app.models.skill import Skill, SkillOrigin
from app.services import file_storage
from app.services.file_storage import SKILL_POLICY
from app.services.skill_bundle import (
    InvalidSkillBundleError as BundleInvalidError,
    normalize_bundle,
    parse_bundle,
    parse_skill_md,
    validate_zip_structure,
)

logger = logging.getLogger(__name__)

# Per-user CUSTOM skill count cap. Bounds account-level abuse (a real
# user has a handful of skills; 20 is comfortable headroom). Paired
# with the per-bundle ``SKILL_POLICY.max_bytes`` cap so total OSS
# storage per user is implicitly bounded too.
_MAX_SKILLS_PER_USER = 20


async def register_skill_bundle(
    db: AsyncSession, *, user_id: int, filename: str, data: bytes,
) -> Skill:
    """Validate, store, and register an uploaded skill bundle.

    Accepts a ``.zip`` or ``.md`` payload, commits the ``skill`` row,
    and returns it refreshed.

    Raises (all :class:`~app.errors.AppError` subclasses, serialized by
    the global handler):

    - ``InvalidSkillBundleError`` (400) — bad extension, malformed
      archive, missing SKILL.md, or unsafe entries.
    - ``SkillBundleTooLargeError`` (400) — over ``SKILL_POLICY.max_bytes``.
    - ``SkillQuotaExceededError`` (403) — at the per-user count cap.
    - ``SkillNameConflictError`` (409) — extracted name collides with an
      existing skill of this user.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SKILL_POLICY.allowed_types:
        raise InvalidSkillBundleError(
            f"unsupported file extension {ext!r}; "
            f"allowed: {', '.join(sorted(SKILL_POLICY.allowed_types))}"
        )
    is_zip = ext == ".zip"

    if len(data) > SKILL_POLICY.max_bytes:
        raise SkillBundleTooLargeError(
            f"file is {len(data)} bytes; limit is {SKILL_POLICY.max_bytes}"
        )

    # Validate structure (zip-bomb, path traversal, symlinks) **before**
    # any path that decompresses entry bytes. ``normalize_bundle``
    # repacks the archive and ``parse_bundle`` reads SKILL.md — both
    # would let a malicious wrapper inflate memory if we let them run
    # on an unvalidated input.
    try:
        if is_zip:
            validate_zip_structure(data)
            normalized = normalize_bundle(data)
            if normalized is not None:
                data = normalized
            parsed = parse_bundle(data)
        else:
            parsed = parse_skill_md(data)
    except BundleInvalidError as exc:
        raise InvalidSkillBundleError(str(exc)) from exc

    await _enforce_user_quota(db, user_id)

    # Upload to OSS under a user-scoped key, then INSERT the row in a
    # try/except that compensates the upload if the INSERT fails (DB
    # constraint, race, transient error). Without this an
    # ``IntegrityError`` would leave an orphan bundle paying OSS storage
    # forever.
    #
    # The filename segment of the key is **derived** from the parsed
    # skill name + canonical (lowercased) extension — NOT the user's
    # original filename. The user-supplied filename was only inspected
    # to decide the ``is_zip`` branch; reusing it verbatim in the key
    # would leak case (``MySkill.ZIP``) and odd characters through to
    # every consumer that dispatches on ``bundle_key.endsWith(".zip")``.
    bundle_filename = f"{parsed.name}{ext}"
    bundle_key = f"{SKILL_POLICY.key_prefix}/{user_id}/{uuid.uuid4()}/{bundle_filename}"
    await asyncio.to_thread(
        file_storage.upload, SKILL_POLICY, bundle_key, data,
    )
    skill = Skill(
        user_id=user_id,
        origin=SkillOrigin.CUSTOM,
        name=parsed.name,
        display_name=parsed.name,
        description=parsed.description,
        bundle_key=bundle_key,
        bundle_size=len(data),
    )
    db.add(skill)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        await _best_effort_delete_oss(bundle_key)
        if isinstance(exc, IntegrityError):
            raise SkillNameConflictError(
                f"a skill named {parsed.name!r} already exists for this user"
            ) from exc
        raise

    await db.refresh(skill)
    logger.info(
        "skill registered: id=%s user_id=%s name=%r size=%d",
        skill.id, user_id, skill.name, skill.bundle_size,
    )
    return skill


async def _enforce_user_quota(db: AsyncSession, user_id: int) -> None:
    """Reject the upload if the user is at the per-user skill count cap.

    Counts the user's CUSTOM rows only — BUILTIN rows have
    ``user_id IS NULL`` so they don't count. Race-acceptable: two
    concurrent uploads at the boundary could both succeed, leaving
    the user one over. Not worth a row lock for a soft policy limit.
    """
    used = await db.scalar(
        select(func.count(Skill.id)).where(Skill.user_id == user_id)
    ) or 0
    if used >= _MAX_SKILLS_PER_USER:
        raise SkillQuotaExceededError(
            f"user already has {used} skills; "
            f"per-user limit is {_MAX_SKILLS_PER_USER}"
        )


async def _best_effort_delete_oss(bundle_key: str) -> None:
    """Compensating delete after a failed register_skill DB write.

    Failures here are logged and swallowed — the DB rolled back, so
    the user-visible state is "upload didn't happen"; an orphan OSS
    object is only a storage-cost issue and should be picked up by a
    bucket lifecycle rule.
    """
    try:
        await asyncio.to_thread(file_storage.delete, SKILL_POLICY, bundle_key)
    except Exception as exc:
        logger.warning(
            "skill upload rollback: OSS cleanup failed for %s: %s",
            bundle_key, exc,
        )
