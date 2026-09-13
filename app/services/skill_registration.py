"""Skill-bundle registration orchestration — the write path behind every
skill upload, whoever is uploading.

Two entry points over one funnel:

- :func:`register_skill_bundle` — a user's own ``CUSTOM`` skill
  (``POST /skills`` on the public surface).
- :func:`register_platform_skill_bundle` — a ``SYSTEM``/``OFFICIAL``
  skill with no owner (``POST /skills`` on the console, and
  ``scripts/upload_skill.py``).

Both validate the bytes, normalize wrapper zips, upload to OSS and write
the row, compensating the OSS upload if the DB write fails. They differ
only in what the repo already says they differ in: ownership, the quota,
where the object lands, and whether a same-named row is a conflict or
the thing being replaced.

Pure byte-level validation lives in :mod:`app.services.skill_bundle`;
this module owns the DB + OSS side effects so endpoints stay thin
adapters.
"""

import asyncio
import logging
import os
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import (
    SkillBundleTooLargeError,
    SkillFileTypeError,
    SkillNameConflictError,
    SkillQuotaExceededError,
)
from app.models.skill import Skill, SkillOrigin, SkillStatus
from app.models.user_skill_preference import UserSkillPreference
from app.services import file_storage
from app.services.file_storage import SKILL_POLICY
from app.services.skill_bundle import (
    ParsedSkill,
    normalize_bundle,
    parse_bundle,
    parse_skill_md,
    validate_zip_structure,
)
from app.services.skill_enablement import legacy_enabled

logger = logging.getLogger(__name__)

# Per-user CUSTOM skill count cap, and the only cap a user's skills have
# left. Bounds account-level abuse — paired with the per-bundle
# ``SKILL_POLICY.max_bytes`` cap so total OSS storage per user is
# implicitly bounded too. Nothing bounds how many a user may *enable*,
# because enabling one lists it in their composer rather than loading it
# into a turn.
_MAX_SKILLS_PER_USER = 100


async def register_skill_bundle(
    db: AsyncSession,
    *,
    user_id: int,
    filename: str,
    data: bytes,
) -> Skill:
    """Validate, store, and register an uploaded skill bundle.

    Accepts a ``.zip`` or ``.md`` payload, commits the ``skill`` row,
    and returns it refreshed.

    Raises (all :class:`~app.errors.AppError` subclasses, serialized by
    the global handler):

    - the ``app.errors`` skill kinds (400) — one per thing the uploader
      would do next: wrong extension, unreadable archive, no SKILL.md,
      frontmatter without a name, body over the cap. Hostile archives stay
      under the generic ``InvalidSkillBundleError``.
    - ``SkillBundleTooLargeError`` (413) — over ``SKILL_POLICY.max_bytes``.
    - ``SkillQuotaExceededError`` (403) — at the per-user count cap.
    - ``SkillNameConflictError`` (409) — extracted name collides with an
      existing skill of this user.

    The row lands ``PUBLISHED`` — a user's own upload has no review to
    wait for — but with **no preference row**, which is how a user who
    has never said reads as off. Turning a skill on puts its metadata in
    every turn's LLM context, so that is an explicit opt-in from the
    skills page, not an upload side effect. The freshly uploaded skill is
    usable immediately via an explicit composer pick, which consults the
    listing's status but not the user's toggle.
    """
    ext = _canonical_ext(filename)
    is_zip = ext == ".zip"
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
    data, parsed = await asyncio.to_thread(
        _validate_and_parse,
        data,
        is_zip=is_zip,
    )

    await _enforce_user_quota(db, user_id)

    # Upload to OSS under a user-scoped key, then INSERT the row in a
    # try/except that compensates the upload if the INSERT fails (DB
    # constraint, race, transient error). Without this an
    # ``IntegrityError`` would leave an orphan bundle paying OSS storage
    # forever.
    #
    # The key's filename segment is **derived** — parsed name + canonical
    # extension, not the user's original filename (see ``_canonical_ext``).
    bundle_key = f"{SKILL_POLICY.key_prefix}/{user_id}/{uuid.uuid4()}/{parsed.name}{ext}"
    await asyncio.to_thread(
        file_storage.upload,
        SKILL_POLICY,
        bundle_key,
        data,
    )
    skill = Skill(
        user_id=user_id,
        origin=SkillOrigin.CUSTOM,
        name=parsed.name,
        display_name=parsed.display_name,
        display_name_i18n=parsed.display_name_i18n,
        description=parsed.description,
        bundle_key=bundle_key,
        bundle_size=len(data),
        status=SkillStatus.PUBLISHED,
        enabled=legacy_enabled(SkillOrigin.CUSTOM, SkillStatus.PUBLISHED),
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
        skill.id,
        user_id,
        skill.name,
        skill.bundle_size,
    )
    return skill


async def register_platform_skill_bundle(
    db: AsyncSession,
    *,
    origin: SkillOrigin,
    filename: str,
    data: bytes,
    publish: bool = False,
    category_id: int | None = None,
) -> tuple[Skill, str | None]:
    """Register (or replace) a platform skill bundle.

    Returns the skill and **the bundle key it displaced** — ``None`` when
    the skill is new. That key is the only remaining pointer to the
    previous version, so the caller is expected to record it.

    A platform skill has **no owner** (``user_id IS NULL``) and so no
    quota — it is the operator's catalogue, not an account's storage.

    **Same name means replace, not conflict.** A user re-uploading a
    name they already own is a mistake worth a 409, but an operator
    uploading ``pdf`` again is shipping a new version of ``pdf`` — the
    row is updated in place, so every user's ``user_skill_preference``
    and the id the worker has seen both survive the update.

    **Flushes but does not commit** — the console's caller is inside
    ``audit()``, which owns the commit so the bundle and its audit row
    land in one transaction. A flush failure compensates the OSS upload
    and re-raises; a failure in the caller's *commit* leaves the object
    for bucket lifecycle to collect, which is the accepted trade (the
    alternative is deleting an object a concurrent turn may be staging).

    The previous bundle object is **left in OSS** on a replace, for that
    same staging reason. The caller records the replaced key in its
    audit payload, which is what makes an unwanted version recoverable.

    **Shipping a bundle is a content change.** A replace leaves the
    listing's ``status`` and every user's preference where they were, so
    an operator fixing a bug cannot pull the skill out from under the
    people using it.

    ``publish`` is the exception, and only because it is not a side
    effect: it is the caller saying so. A new row lands ``STAGED`` —
    uploading is not publishing, and the one thing an upload cannot undo
    is having been visible — and ``publish=True`` says to go live, on a
    new row or an existing one alike. Without that second half, a
    deployment whose only publish path is this script (hkgai has no
    console) could stage a listing and never get it live.

    ``category_id`` is tri-state: ``None`` leaves the row alone, so
    re-uploading a bundle keeps the tab an operator filed it under. It
    has no per-origin default — a new skill is simply uncategorised
    until someone files it.

    Raises the same typed kinds the user path does.
    """
    if origin is SkillOrigin.CUSTOM:
        raise ValueError("CUSTOM skills are owned by a user; use register_skill_bundle")

    ext = _canonical_ext(filename)
    is_zip = ext == ".zip"
    if len(data) > SKILL_POLICY.max_bytes:
        raise SkillBundleTooLargeError(
            f"file is {len(data)} bytes; limit is {SKILL_POLICY.max_bytes}"
        )
    data, parsed = await asyncio.to_thread(
        _validate_and_parse,
        data,
        is_zip=is_zip,
    )

    # Looked up before the upload so a failure here costs no OSS object.
    skill = await db.scalar(select(Skill).where(Skill.user_id.is_(None), Skill.name == parsed.name))
    replaced_key = skill.bundle_key if skill is not None else None
    became_agent_only = (
        skill is not None
        and origin is SkillOrigin.SYSTEM
        and skill.origin is not SkillOrigin.SYSTEM
    )

    # Keyed by origin rather than by owner (there is none) — the same
    # scheme ``scripts/upload_skill.py`` has always written, so bundles
    # from either caller are indistinguishable in the bucket.
    bundle_key = f"{origin.value.lower()}/{parsed.name}/{uuid.uuid4()}/{parsed.name}{ext}"
    await asyncio.to_thread(file_storage.upload, SKILL_POLICY, bundle_key, data)
    if skill is None:
        skill = Skill(user_id=None, name=parsed.name, status=SkillStatus.STAGED)
        db.add(skill)
    if publish:
        skill.status = SkillStatus.PUBLISHED
    if became_agent_only:
        # An existing listing turning agent-only strands whatever its
        # users had said about it: nothing lists a SYSTEM skill, so
        # nothing can unsay it, and a stale ``enabled = false`` would
        # hide a core skill from them for good. Dropping those rows is
        # what keeps "a preference row exists only for a skill its user
        # can see" true rather than merely usual.
        await db.execute(
            delete(UserSkillPreference).where(UserSkillPreference.skill_id == skill.id)
        )
    skill.origin = origin
    skill.display_name = parsed.display_name
    skill.display_name_i18n = parsed.display_name_i18n
    skill.description = parsed.description
    skill.bundle_key = bundle_key
    skill.bundle_size = len(data)
    skill.enabled = legacy_enabled(origin, skill.status)
    if category_id is not None:
        skill.category_id = category_id

    try:
        await db.flush()
    except Exception:
        await db.rollback()
        await _best_effort_delete_oss(bundle_key)
        raise

    logger.info(
        "platform skill %s: id=%s origin=%s name=%r size=%d status=%s",
        "replaced" if replaced_key else "registered",
        skill.id,
        origin.value,
        skill.name,
        skill.bundle_size,
        skill.status.value,
    )
    return skill, replaced_key


def _canonical_ext(filename: str) -> str:
    """Return the bundle's canonical extension, rejecting unknown ones.

    A ``.skill`` file is a renamed zip (the portable Agent Skills package
    extension), so it canonicalizes to ``.zip`` — consumers, notably the
    worker's staging step, dispatch on that suffix. Callers therefore
    test ``ext == ".zip"`` rather than tracking a separate flag.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SKILL_POLICY.allowed_types:
        raise SkillFileTypeError(
            f"unsupported file extension {ext!r}; "
            f"allowed: {', '.join(sorted(SKILL_POLICY.allowed_types))}"
        )
    return ".zip" if ext in (".zip", ".skill") else ext


def _validate_and_parse(data: bytes, *, is_zip: bool) -> tuple[bytes, ParsedSkill]:
    """Structural validation + SKILL.md parse, off the event loop.

    Returns the (possibly repacked) bundle bytes and the parsed
    metadata. For a zip: validate structure FIRST — ``validate_zip_structure``
    is header-only and rejects zip-bombs / traversal / symlinks before
    ``normalize_bundle`` repacks or ``parse_bundle`` reads any entry, so an
    unvalidated archive never reaches a path that allocates decompressed
    bytes. For a bare ``.md`` the bytes are the SKILL.md content directly.

    Synchronous by design (CPU-bound) — the caller runs it via
    :func:`asyncio.to_thread`. Structural and parse failures raise their
    own ``app.errors`` kinds from :mod:`app.services.skill_bundle`, which
    travel up untouched — there is nothing for this layer to translate.
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
    used = await db.scalar(select(func.count(Skill.id)).where(Skill.user_id == user_id)) or 0
    if used >= _MAX_SKILLS_PER_USER:
        raise SkillQuotaExceededError(
            f"user already has {used} skills; per-user limit is {_MAX_SKILLS_PER_USER}"
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
            bundle_key,
            exc,
        )
