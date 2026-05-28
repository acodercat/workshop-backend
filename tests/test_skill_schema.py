"""Schema validation for the /api/skills surface."""

from app.schemas.skill import SkillUpdate


class TestSkillUpdate:
    def test_name_not_exposed(self):
        """``name`` is immutable (derived from bundle SKILL.md) — the
        update schema must not accept it."""
        assert "name" not in SkillUpdate.model_fields
