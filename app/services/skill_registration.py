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
    ParsedSkill,
    normalize_bundle,
    parse_bundle,
    parse_skill_md,
    validate_zip_structure,
)

logger = logging.getLogger(__name__)

# Per-user CUSTOM skill count cap. Bounds account-level abuse only —
# paired with the per-bundle ``SKILL_POLICY.max_bytes`` cap so total
# OSS storage per user is implicitly bounded too. The per-turn LLM
# context cost is bounded separately (and much lower) by
# ``skill_enablement.MAX_ENABLED_SKILLS``.
_MAX_SKILLS_PER_USER = 100


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
    - ``SkillBundleTooLargeError`` (413) — over ``SKILL_POLICY.max_bytes``.
    - ``SkillQuotaExceededError`` (403) — at the per-user count cap.
    - ``SkillNameConflictError`` (409) — extracted name collides with an
      existing skill of this user.

    The row lands **disabled**: enabling a skill puts its metadata in
    every turn's LLM context, so it's an explicit user opt-in (from the
    skills page), not an upload side effect — same posture as OFFICIAL
    skills. The freshly uploaded skill is still usable immediately via
    an explicit composer pick, which doesn't consult the toggle.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SKILL_POLICY.allowed_types:
        raise InvalidSkillBundleError(
            f"unsupported file extension {ext!r}; "
            f"allowed: {', '.join(sorted(SKILL_POLICY.allowed_types))}"
        )
    # A ``.skill`` file is just a renamed zip (the portable Agent Skills
    # package extension): treat it as a zip and canonicalize ``ext`` to
    # ``.zip`` for the derived key (see the bundle-key comment below).
    is_zip = ext in (".zip", ".skill")
    if is_zip:
        ext = ".zip"

    if len(data) > SKILL_POLICY.max_bytes:
        raise SkillBundleTooLargeError(
            f"file is {len(data)} bytes; limit is {SKILL_POLICY.max_bytes}"
        )

    # Validate structure (zip-bomb, path traversal, symlinks) **before**
    # any path that decompresses entry bytes, then parse SKILL.md. The
    # whole block is CPU-bound (zip header inspection, an in-memory repack
    # for wrapper bundles, a UTF-8 decode) and runs on a worst-case
    # ``SKILL_POLICY.max_bytes`` (50 MiB) input, so it goes through
    # ``asyncio.to_thread`` — otherwise a single large bundle would stall
    # the event loop (and with it every SSE stream / enqueue on this
    # process) for the duration of the unpack.
    try:
        data, parsed = await asyncio.to_thread(
            _validate_and_parse, data, is_zip=is_zip,
        )
    except BundleInvalidError as exc:
        raise InvalidSkillBundleError(str(exc)) from exc

    await _enforce_user_quota(db, user_id)

    # Upload to OSS under a user-scoped key, then INSERT the row in a
    # try/except that compensates the upload if the INSERT fails (DB
    # constraint, race, transient error). Without this an
    # ``IntegrityError`` would leave an orphan bundle paying OSS storage
    # forever.
    #
    # The key's filename segment is **derived** — parsed name + canonical
    # extension, not the user's original filename. The zip family
    # (``.zip``/``.skill``, any case) is canonicalized to ``.zip`` above;
    # ``.md`` keeps its lowercased ext. This matters because consumers
    # (notably the worker's staging) dispatch on the ``.zip`` suffix.
    bundle_filename = f"{parsed.name}{ext}"
    bundle_key = f"{SKILL_POLICY.key_prefix}/{user_id}/{uuid.uuid4()}/{bundle_filename}"
    await asyncio.to_thread(
        file_storage.upload, SKILL_POLICY, bundle_key, data,
    )
    # ``enabled=False`` is explicit: the column's ``server_default`` is
    # ``true``, so omitting it would enable the skill. Uploads land
    # disabled (rationale in the docstring).
    skill = Skill(
        user_id=user_id,
        origin=SkillOrigin.CUSTOM,
        name=parsed.name,
        display_name=parsed.display_name,
        display_name_i18n=parsed.display_name_i18n,
        description=parsed.description,
        bundle_key=bundle_key,
        bundle_size=len(data),
        enabled=False,
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


def _validate_and_parse(data: bytes, *, is_zip: bool) -> tuple[bytes, ParsedSkill]:
    """Structural validation + SKILL.md parse, off the event loop.

    Returns the (possibly repacked) bundle bytes and the parsed
    metadata. For a zip: validate structure FIRST — ``validate_zip_structure``
    is header-only and rejects zip-bombs / traversal / symlinks before
    ``normalize_bundle`` repacks or ``parse_bundle`` reads any entry, so an
    unvalidated archive never reaches a path that allocates decompressed
    bytes. For a bare ``.md`` the bytes are the SKILL.md content directly.

    Synchronous by design (CPU-bound) — the caller runs it via
    :func:`asyncio.to_thread`. Raises :class:`BundleInvalidError` on any
    structural / parse failure; the caller maps that to the typed
    ``InvalidSkillBundleError``.
    """
    if is_zip:
        validate_zip_structure(data)
        normalized = normalize_bundle(data)
        if normalized is not None:
            data = normalized
        return data, parse_bundle(data)
    return data, parse_skill_md(data)


async def _enforce_user_quota(db: AsyncSession, user_id: int) -> None:
    """Reject the upload if the user is at the per-user skill count cap.

    Counts the user's CUSTOM rows only — platform rows have
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
