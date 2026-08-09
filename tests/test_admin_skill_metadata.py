"""``PATCH /skills/{id}`` — the console's edit of a skill's picker metadata.

The public surface calls ``display_name`` / ``description`` editable and
refuses them on a platform skill because they would "mutate the shared
row". The console owns that row, so it is the surface that can — and it
has to carry the same locale-map rule the public PATCH does.
"""

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import skill as admin_skill
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.admin_audit_log import AdminAuditLog
from app.models.skill import Skill, SkillOrigin
from app.models.staff import Staff, StaffStatus

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


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

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def _skill(**over) -> Skill:
    skill = Skill(
        **{
            "id": 5, "user_id": None, "origin": SkillOrigin.OFFICIAL,
            "name": "pdf", "display_name": "PDF", "description": "reads pdfs",
            "enabled": False, "bundle_key": "official/pdf/x/pdf.zip",
            "bundle_size": 10, "display_name_i18n": {"zh": "PDF 工具"},
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
        id=7, username="alice", password_hash="x", name="Alice",
        status=StaffStatus.ACTIVE, token_version=0,
    )
    return TestClient(app)


def _audit_rows(db: _FakeDb) -> list[AdminAuditLog]:
    return [obj for obj in db.added if isinstance(obj, AdminAuditLog)]


def test_a_rename_clears_the_bundle_locale_map() -> None:
    """The public list endpoint resolves ``display_name_i18n`` first, so a
    stale map would silently shadow the new name — the public PATCH clears
    it on rename and so must this one."""
    skill = _skill()
    db = _FakeDb(skill)

    response = _client(db).patch(
        "/skills/5", json={"display_name": "PDF Tools", "reason": "TICKET-1"}
    )

    assert response.status_code == 200
    assert skill.display_name == "PDF Tools"
    assert skill.display_name_i18n is None


def test_editing_only_the_description_leaves_the_locale_map() -> None:
    skill = _skill()
    db = _FakeDb(skill)

    _client(db).patch(
        "/skills/5", json={"description": "reads and writes pdfs", "reason": "r"}
    )

    assert skill.description == "reads and writes pdfs"
    assert skill.display_name_i18n == {"zh": "PDF 工具"}


def test_the_audit_payload_records_each_changed_field() -> None:
    db = _FakeDb(_skill())

    _client(db).patch(
        "/skills/5",
        json={"display_name": "PDF Tools", "enabled": True, "reason": "TICKET-1"},
    )

    (row,) = _audit_rows(db)
    assert row.action == "skill.update"
    assert row.payload["display_name"] == {"from": "PDF", "to": "PDF Tools"}
    assert row.payload["enabled"] == {"from": False, "to": True}
    assert "description" not in row.payload  # unsent, so unchanged
    assert db.commits == 1


def test_an_edit_that_changes_nothing_writes_no_audit_row() -> None:
    db = _FakeDb(_skill())

    response = _client(db).patch(
        "/skills/5", json={"display_name": "PDF", "enabled": False, "reason": "r"}
    )

    assert response.status_code == 200
    assert _audit_rows(db) == []
    assert db.commits == 0


def test_the_bundle_name_cannot_be_edited() -> None:
    """``name`` is how the worker finds the staged directory and comes from
    SKILL.md — it changes by re-uploading and no other way."""
    skill = _skill()
    db = _FakeDb(skill)

    _client(db).patch("/skills/5", json={"name": "not-pdf", "reason": "r"})

    assert skill.name == "pdf"
