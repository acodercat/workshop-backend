"""Per-user cap on *enabled* skills — the ambient-context budget.

Every effective-enabled skill (own CUSTOM + opted-in OFFICIAL) costs
one metadata line in the LLM context every turn via the worker's
``use_skill`` listing, so this cap bounds per-turn context growth the
way the upload quota bounds storage. Explicit per-turn composer picks
are deliberately *not* limited — an unselected skill costs nothing.

Effective enabled is ``COALESCE(pref.enabled, skill.enabled)`` — the
same expression as ``GET /skills``, the PATCH toggle, and the worker's
``list_enabled``. SYSTEM rows never count: they're agent-only and not
user-controllable, so they can't be traded against the budget.
"""

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import SkillEnableQuotaExceededError
from app.models.skill import Skill, SkillOrigin
from app.models.user_skill_preference import UserSkillPreference

# Cap on simultaneously enabled skills per user. Sized to keep the
# worst-case ``use_skill`` listing around 1-2k tokens per turn.
MAX_ENABLED_SKILLS = 20


async def count_enabled_skills(
    db: AsyncSession,
    user_id: int,
    *,
    exclude_skill_id: int | None = None,
) -> int:
    """Count the user's effective-enabled skills (CUSTOM + OFFICIAL).

    ``exclude_skill_id`` leaves one skill out of the count so an
    enable-toggle check can ask "how many *others* are on?" — making
    re-enabling an already-enabled skill a no-op instead of a
    spurious cap rejection.
    """
    stmt = (
        select(func.count())
        .select_from(Skill)
        .outerjoin(
            UserSkillPreference,
            and_(
                UserSkillPreference.skill_id == Skill.id,
                UserSkillPreference.user_id == user_id,
            ),
        )
        .where(
            (Skill.user_id == user_id) | (Skill.origin == SkillOrigin.OFFICIAL),
            func.coalesce(UserSkillPreference.enabled, Skill.enabled),
        )
    )
    if exclude_skill_id is not None:
        stmt = stmt.where(Skill.id != exclude_skill_id)
    return await db.scalar(stmt) or 0


async def ensure_enable_capacity(
    db: AsyncSession,
    user_id: int,
    *,
    exclude_skill_id: int | None = None,
) -> None:
    """Raise :class:`SkillEnableQuotaExceededError` if enabling one more
    skill would push the user past :data:`MAX_ENABLED_SKILLS`.

    Race-acceptable: two concurrent enables at the boundary could both
    pass, leaving the user one over. Not worth a row lock for a soft
    context-budget limit (same posture as the upload quota check).
    """
    used = await count_enabled_skills(
        db,
        user_id,
        exclude_skill_id=exclude_skill_id,
    )
    if used >= MAX_ENABLED_SKILLS:
        raise SkillEnableQuotaExceededError(
            f"user already has {used} skills enabled; limit is {MAX_ENABLED_SKILLS}"
        )
