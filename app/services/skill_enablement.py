"""A user's own answer about a skill: the join it is listed through, and
its reads and writes.

Two rows decide whether a user is *offered* a skill: ``skill.status`` for
whether the listing is live, and ``user_skill_preference.enabled`` for
whether they keep it in their composer picker, where no row means no.
Nothing combines them, which is why nothing here does — ``status`` is
checked wherever a skill is looked up, and this row is read only to report
where the toggle sits and written only when a user moves it.

Whether a skill then *reaches the model* is a third question neither row
answers: a turn carries the agent-only ``SYSTEM`` set (the worker's
``skill_repository``) plus what the user picked (``session.enqueue``).
That is also why no cap bounds how many skills a user may turn on. The old
limit of 20 bounded per-turn context, back when every enabled skill spent a
metadata line in the ``use_skill`` listing; once the listing stopped being
fed from this row, a limit here would have been rationing something free.
What a turn does spend is bounded where it is chosen, by the cap on
``MessageCreate.skills``.
"""

from sqlalchemy import ColumnElement, and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill
from app.models.user_skill_preference import UserSkillPreference


def preference_join(user_id: int) -> ColumnElement[bool]:
    """ON clause pairing each skill with *user_id*'s answer, if any.

    Always an ``outerjoin``: no row is the common case, and it means off.
    """
    return and_(
        UserSkillPreference.skill_id == Skill.id,
        UserSkillPreference.user_id == user_id,
    )


async def is_enabled(db: AsyncSession, user_id: int, skill_id: int) -> bool:
    """*user_id*'s answer for one skill; ``False`` where they never gave one."""
    return bool(
        await db.scalar(
            select(UserSkillPreference.enabled).where(
                UserSkillPreference.user_id == user_id,
                UserSkillPreference.skill_id == skill_id,
            )
        )
    )


async def set_enabled(db: AsyncSession, user_id: int, skill_id: int, enabled: bool) -> None:
    """Record *user_id*'s answer for one skill, whether or not they had one.

    ``updated_at`` is bumped explicitly in the conflict branch — the
    ``onupdate=func.now()`` mapping on the model only fires on ORM
    UPDATE statements, not on Postgres ``ON CONFLICT DO UPDATE``.
    """
    await db.execute(
        pg_insert(UserSkillPreference)
        .values(user_id=user_id, skill_id=skill_id, enabled=enabled)
        .on_conflict_do_update(
            index_elements=[
                UserSkillPreference.user_id,
                UserSkillPreference.skill_id,
            ],
            set_={"enabled": enabled, "updated_at": func.now()},
        )
    )


async def clear_for_skill(db: AsyncSession, skill_id: int) -> None:
    """Drop every user's answer for one skill — for a listing no user can
    see any more, whose answers nothing could show or change again."""
    await db.execute(delete(UserSkillPreference).where(UserSkillPreference.skill_id == skill_id))
