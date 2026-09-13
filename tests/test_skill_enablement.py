"""The context-budget count, and the join it reads a user's answer through.

The predicate is built from parts that are each easy to drop and whose
loss is silent: a skill that quietly stops reaching the model looks
exactly like a model that chose not to use it. These compile the
statement and read the SQL back, which is what makes a wrong edit
visible without a database.

The same gate expressed for the agent — which lists ``SYSTEM`` skills
and so needs the exemption this side never does — lives in the worker's
``test_skill_repository``.
"""

from sqlalchemy.dialects import postgresql

from app.models.skill import SkillOrigin, SkillStatus
from app.services.skill_enablement import count_enabled_skills, legacy_enabled

_USER_ID = 7


class _CapturingDb:
    """Answers a count with zero and keeps the SQL that asked."""

    def __init__(self) -> None:
        self.sql = ""

    async def scalar(self, stmt) -> int:
        self.sql = str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        return 0


async def _count_sql(**kwargs) -> str:
    db = _CapturingDb()
    await count_enabled_skills(db, _USER_ID, **kwargs)
    return db.sql


class TestTheBudgetCount:
    async def test_both_questions_are_asked(self) -> None:
        """Liveness and the user's answer, and neither alone."""
        sql = await _count_sql()

        assert "skill.status = 'PUBLISHED'" in sql
        assert "coalesce(user_skill_preference.enabled, false)" in sql

    async def test_a_missing_preference_row_reads_as_off(self) -> None:
        """No row is the common case and it must not mean "on".

        The column this replaced defaulted the other way, which is how
        rows nobody opted into ended up in every turn's context.
        """
        assert "coalesce(user_skill_preference.enabled, false)" in await _count_sql()

    async def test_the_join_is_scoped_to_one_user(self) -> None:
        """Without the user_id predicate every user's row would match."""
        assert f"user_skill_preference.user_id = {_USER_ID}" in await _count_sql()

    async def test_system_is_outside_the_budget(self) -> None:
        """Agent-only skills cannot be traded against a cap the user owns.

        The scope keeps them out, so the count needs no exemption for
        them — and must not grow one, which would let an operator's
        decision eat a user's allowance.
        """
        sql = await _count_sql()

        assert "skill.origin = 'OFFICIAL'" in sql
        assert "SYSTEM" not in sql

    async def test_one_skill_can_be_left_out(self) -> None:
        """So re-enabling an already-on skill is a no-op, not a rejection."""
        assert "skill.id != 5" in await _count_sql(exclude_skill_id=5)


class TestLegacyEnabled:
    """The transitional bridge, deleted with the column it feeds."""

    def test_system_mirrors_liveness(self) -> None:
        # SYSTEM has no preference rows, so the old worker reads this
        # column directly and it has to carry the whole answer.
        assert legacy_enabled(SkillOrigin.SYSTEM, SkillStatus.PUBLISHED) is True
        assert legacy_enabled(SkillOrigin.SYSTEM, SkillStatus.STAGED) is False
        assert legacy_enabled(SkillOrigin.SYSTEM, SkillStatus.RETIRED) is False

    def test_the_other_origins_are_inert(self) -> None:
        # Both always have a preference row after the migration, so the
        # old formula never falls through to the column.
        for origin in (SkillOrigin.OFFICIAL, SkillOrigin.CUSTOM):
            for status in SkillStatus:
                assert legacy_enabled(origin, status) is False
