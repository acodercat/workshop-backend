"""The join a user's own answer is read through, and the legacy mirror.

The count that used to live here went with the cap on enabled skills:
turning a skill on lists it in the composer rather than loading it, so
there is no per-turn budget left to ration. What remains is the join
every read of that answer goes through — easy to get subtly wrong, and
wrong in a way that reads as plausible: a join missing its ``user_id``
predicate would show one user another's choices.

What the agent may invoke — which needs the ``SYSTEM`` exemption this
side never does — is pinned in the shared worker's
``test_skill_repository``.
"""

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models.skill import Skill, SkillOrigin, SkillStatus
from app.models.user_skill_preference import UserSkillPreference
from app.services.skill_enablement import legacy_enabled, preference_join

_USER_ID = 7


def _join_sql(user_id: int) -> str:
    """The ON clause as Postgres will see it."""
    stmt = (
        select(UserSkillPreference.enabled)
        .select_from(Skill)
        .outerjoin(UserSkillPreference, preference_join(user_id))
    )
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class TestPreferenceJoin:
    def test_it_pairs_the_row_with_its_skill(self) -> None:
        assert "user_skill_preference.skill_id = skill.id" in _join_sql(_USER_ID)

    def test_it_is_scoped_to_one_user(self) -> None:
        """Without this predicate the join matches every user's row, and
        the first one back decides what this user is shown."""
        assert f"user_skill_preference.user_id = {_USER_ID}" in _join_sql(_USER_ID)

    def test_it_stays_an_outer_join(self) -> None:
        """No row is the common case and means "never said", so an inner
        join would drop every skill the user has not touched — the whole
        catalogue, for most users."""
        assert "LEFT OUTER JOIN" in _join_sql(_USER_ID)


class TestLegacyEnabled:
    """The transitional bridge, deleted with the column it feeds."""

    def test_system_mirrors_liveness(self) -> None:
        # SYSTEM has no preference rows, so a pre-``status`` worker reads
        # this column directly and it has to carry the whole answer.
        assert legacy_enabled(SkillOrigin.SYSTEM, SkillStatus.PUBLISHED) is True
        assert legacy_enabled(SkillOrigin.SYSTEM, SkillStatus.STAGED) is False
        assert legacy_enabled(SkillOrigin.SYSTEM, SkillStatus.RETIRED) is False

    def test_the_other_origins_are_inert(self) -> None:
        # Both always have a preference row after the migration, so the
        # old formula never falls through to the column.
        for origin in (SkillOrigin.OFFICIAL, SkillOrigin.CUSTOM):
            for status in SkillStatus:
                assert legacy_enabled(origin, status) is False
