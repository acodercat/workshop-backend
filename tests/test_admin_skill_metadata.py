"""``PATCH /skills/{id}`` — the console's edit of a skill's picker metadata.

The public surface calls ``display_name`` / ``description`` editable and
refuses them on a platform skill because they would "mutate the shared
row". The console owns that row, so it is the surface that can — and it
has to carry the same locale-map rule the public PATCH does.
"""

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import skill as admin_skill
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.skill import Skill, SkillOrigin, SkillStatus
from app.models.staff import Staff, StaffStatus

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeDb:
    def __init__(self, skill: Skill) -> None:
        self.skill = skill
        self.added: list = []
        self.commits = 0

    async def get(self, _model, _pk):
        return self.skill

    async def scalar(self, _stmt):
        return None  # platform rows have no owner

    def add(self, obj) -> None:
        self.added.append(obj)

    async def execute(self, _stmt):
        class _Empty:
            def first(self):
                return None

            def one_or_none(self):
                return None

        return _Empty()

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def _skill(**over) -> Skill:
    skill = Skill(
        **{
            "id": 5,
            "user_id": None,
            "origin": SkillOrigin.OFFICIAL,
            "name": "pdf",
            "display_name": "PDF",
            "description": "reads pdfs",
            "status": SkillStatus.STAGED,
            "enabled": False,
            "bundle_key": "official/pdf/x/pdf.zip",
            "bundle_size": 10,
            "display_name_i18n": {"zh": "PDF 工具"},
            **over,
        }
    )
    skill.created_at = skill.updated_at = _NOW
    return skill


def _client(db: _FakeDb) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_skill.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[authenticate_and_get_staff] = lambda: Staff(
        id=7,
        username="alice",
        password_hash="x",
        name="Alice",
        status=StaffStatus.ACTIVE,
        token_version=0,
    )
    return TestClient(app)


def test_a_rename_clears_the_bundle_locale_map() -> None:
    """The public list endpoint resolves ``display_name_i18n`` first, so a
    stale map would silently shadow the new name — the public PATCH clears
    it on rename and so must this one."""
    skill = _skill()
    db = _FakeDb(skill)

    response = _client(db).patch("/skills/5", json={"display_name": "PDF Tools"})

    assert response.status_code == 200
    assert skill.display_name == "PDF Tools"
    assert skill.display_name_i18n is None


def test_editing_only_the_description_leaves_the_locale_map() -> None:
    skill = _skill()
    db = _FakeDb(skill)

    _client(db).patch("/skills/5", json={"description": "reads and writes pdfs"})

    assert skill.description == "reads and writes pdfs"
    assert skill.display_name_i18n == {"zh": "PDF 工具"}


def test_each_changed_field_is_applied_in_one_commit() -> None:
    skill = _skill()
    db = _FakeDb(skill)

    _client(db).patch(
        "/skills/5",
        json={"display_name": "PDF Tools", "status": "PUBLISHED"},
    )

    assert skill.display_name == "PDF Tools"
    assert skill.status == SkillStatus.PUBLISHED
    assert skill.description == "reads pdfs"  # unsent, so unchanged
    assert db.commits == 1


def test_an_edit_that_changes_nothing_writes_nothing() -> None:
    db = _FakeDb(_skill())

    response = _client(db).patch("/skills/5", json={"display_name": "PDF", "status": "STAGED"})

    assert response.status_code == 200
    assert db.commits == 0


def test_the_bundle_name_cannot_be_edited() -> None:
    """``name`` is how the worker finds the staged directory and comes from
    SKILL.md — it changes by re-uploading and no other way."""
    skill = _skill()
    db = _FakeDb(skill)

    response = _client(db).patch("/skills/5", json={"name": "not-pdf", "display_name": "PDF Tools"})

    assert response.status_code == 200
    # The rename rode along with a change that *is* accepted, and was
    # dropped on its own.
    assert skill.display_name == "PDF Tools"
    assert skill.name == "pdf"


def test_an_empty_patch_is_refused() -> None:
    """Every field is optional, so without ``_at_least_one_change`` an
    empty body would be a valid PATCH that the no-op guard then answers
    200 — a request that did nothing, reported as success."""
    db = _FakeDb(_skill())

    response = _client(db).patch("/skills/5", json={})

    assert response.status_code == 422
    assert db.commits == 0


def test_a_users_own_skill_cannot_be_retired_from_here() -> None:
    """The console does not touch a user's own file — the same boundary
    the download route draws. Retiring one would strand it in a state
    its owner cannot see, edit, delete or stop paying quota for."""
    db = _FakeDb(_skill(origin=SkillOrigin.CUSTOM, user_id=42))

    response = _client(db).patch("/skills/5", json={"status": "RETIRED"})

    assert response.status_code == 403
    assert db.commits == 0


def test_a_users_own_skill_can_still_have_its_metadata_edited() -> None:
    """Only the lifecycle is refused; the picker text is this surface's."""
    skill = _skill(origin=SkillOrigin.CUSTOM, user_id=42)
    db = _FakeDb(skill)

    response = _client(db).patch("/skills/5", json={"display_name": "Renamed"})

    assert response.status_code == 200
    assert skill.display_name == "Renamed"
