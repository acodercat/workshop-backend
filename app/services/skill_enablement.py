"""The join a user's own answer about a skill is read through.

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

from sqlalchemy import ColumnElement, and_

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
