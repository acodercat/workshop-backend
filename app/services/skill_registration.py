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
    # extension, not the user's original filename (see ``_canonical_ext``).
    bundle_key = (
        f"{SKILL_POLICY.key_prefix}/{user_id}/{uuid.uuid4()}/{parsed.name}{ext}"
    )
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


async def register_platform_skill_bundle(
    db: AsyncSession,
    *,
    origin: SkillOrigin,
    filename: str,
    data: bytes,
    enabled: bool | None = None,
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

    ``enabled`` defaults to the origin's rule: ``SYSTEM`` lands enabled
    (agent-only, and disabled means invisible to the agent), ``OFFICIAL``
    lands disabled because ``skill.enabled`` is the default every user
    who never touched the toggle falls back to — an enabled-by-default
    official skill would push its metadata into every user's every-turn
    LLM context. Pass ``enabled=False`` to stage a ``SYSTEM`` bundle
    without exposing it.

    Raises ``InvalidSkillBundleError`` (400) / ``SkillBundleTooLargeError``
    (413) exactly as the user path does.
    """
    if origin is SkillOrigin.CUSTOM:
        raise ValueError(
            "CUSTOM skills are owned by a user; use register_skill_bundle"
        )

    ext = _canonical_ext(filename)
    is_zip = ext == ".zip"
    if len(data) > SKILL_POLICY.max_bytes:
        raise SkillBundleTooLargeError(
            f"file is {len(data)} bytes; limit is {SKILL_POLICY.max_bytes}"
        )
    try:
        data, parsed = await asyncio.to_thread(
            _validate_and_parse, data, is_zip=is_zip,
        )
    except BundleInvalidError as exc:
        raise InvalidSkillBundleError(str(exc)) from exc

    if enabled is None:
        enabled = origin is SkillOrigin.SYSTEM

    # Keyed by origin rather than by owner (there is none) — the same
    # scheme ``scripts/upload_skill.py`` has always written, so bundles
    # from either caller are indistinguishable in the bucket.
    bundle_key = (
        f"{origin.value.lower()}/{parsed.name}/{uuid.uuid4()}/{parsed.name}{ext}"
    )
    await asyncio.to_thread(file_storage.upload, SKILL_POLICY, bundle_key, data)

    skill = await db.scalar(
        select(Skill).where(Skill.user_id.is_(None), Skill.name == parsed.name)
    )
    replaced_key = skill.bundle_key if skill is not None else None
    if skill is None:
        skill = Skill(user_id=None, name=parsed.name)
        db.add(skill)
    skill.origin = origin
    skill.display_name = parsed.display_name
    skill.display_name_i18n = parsed.display_name_i18n
    skill.description = parsed.description
    skill.bundle_key = bundle_key
    skill.bundle_size = len(data)
    skill.enabled = enabled

    try:
        await db.flush()
    except Exception:
        await db.rollback()
        await _best_effort_delete_oss(bundle_key)
        raise

    logger.info(
        "platform skill %s: id=%s origin=%s name=%r size=%d enabled=%s",
        "replaced" if replaced_key else "registered",
        skill.id, origin.value, skill.name, skill.bundle_size, enabled,
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
        raise InvalidSkillBundleError(
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
