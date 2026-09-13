"""The per-user cap on enabled skills, and the join it reads answers through.

Whether a skill reaches a user turns on two rows: ``skill.status`` for
whether the listing is live, and ``user_skill_preference.enabled`` for
whether this user wants it, where having no row means no. Only the
worker asks both at once — it lists agent-only ``SYSTEM`` skills, which
have no user side to consult — so the whole expression lives there, and
there is no copy here to drift from it.

The cap is this module's own subject. Each enabled skill costs a
metadata line in the LLM context every turn via the worker's
``use_skill`` listing, so it bounds per-turn context growth the way the
upload quota bounds storage. Per-turn composer picks are deliberately
not limited — an unselected skill costs nothing, and a pick is its own
authorization.
"""

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import SkillEnableQuotaExceededError
from app.models.skill import Skill, SkillOrigin, SkillStatus
from app.models.user_skill_preference import UserSkillPreference

# Cap on simultaneously enabled skills per user. Sized to keep the
# worst-case ``use_skill`` listing around 1-2k tokens per turn.
MAX_ENABLED_SKILLS = 20


def preference_join(user_id: int) -> ColumnElement[bool]:
    """ON clause pairing each skill with *user_id*'s answer, if any.

    Always an ``outerjoin``: no row is the common case, and it means off.
    """
    return and_(
        UserSkillPreference.skill_id == Skill.id,
        UserSkillPreference.user_id == user_id,
    )


def legacy_enabled(origin: SkillOrigin, status: SkillStatus) -> bool:
    """TRANSITIONAL — the value that keeps the pre-``status`` worker right.

    ``core/agent-worker`` is shared with metaseq's deployment and still
    gates on ``COALESCE(pref.enabled, skill.enabled)``, which it cannot
    stop doing until both databases carry ``status``. One value serves
    every origin until then: ``SYSTEM`` has no preference rows, so the
    old formula reads this column and it must mirror liveness; everyone
    else either has a row it never looks past, or is legitimately off.

    Deleted with the column in the follow-up migration.
    """
    return origin is SkillOrigin.SYSTEM and status is SkillStatus.PUBLISHED


async def count_enabled_skills(
    db: AsyncSession, user_id: int, *, exclude_skill_id: int | None = None,
) -> int:
    """Count what this user is spending the context budget on.

    Scoped to the user-facing catalogue — own ``CUSTOM`` plus
    ``OFFICIAL``. ``SYSTEM`` is agent-only and not user-controllable, so
    there is nothing to trade against the budget and the scope cannot
    match one.

    ``exclude_skill_id`` leaves one skill out so an enable-toggle check
    can ask "how many *others* are on?" — making re-enabling an
    already-enabled skill a no-op rather than a spurious rejection.
    """
    stmt = (
        select(func.count())
        .select_from(Skill)
        .outerjoin(UserSkillPreference, preference_join(user_id))
        .where(
            (Skill.user_id == user_id)
            | (Skill.origin == SkillOrigin.OFFICIAL),
            Skill.status == SkillStatus.PUBLISHED,
            func.coalesce(UserSkillPreference.enabled, False),
        )
    )
    if exclude_skill_id is not None:
        stmt = stmt.where(Skill.id != exclude_skill_id)
    return await db.scalar(stmt) or 0


async def ensure_enable_capacity(
    db: AsyncSession, user_id: int, *, exclude_skill_id: int | None = None,
) -> None:
    """Raise :class:`SkillEnableQuotaExceededError` if enabling one more
    skill would push the user past :data:`MAX_ENABLED_SKILLS`.

    Race-acceptable: two concurrent enables at the boundary could both
    pass, leaving the user one over. Not worth a row lock for a soft
    context-budget limit (same posture as the upload quota check).
    """
    used = await count_enabled_skills(
        db, user_id, exclude_skill_id=exclude_skill_id,
    )
    if used >= MAX_ENABLED_SKILLS:
        raise SkillEnableQuotaExceededError(
            f"user already has {used} skills enabled; "
            f"limit is {MAX_ENABLED_SKILLS}"
        )
