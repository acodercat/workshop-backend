"""``POST /skills`` — the console's platform-bundle upload.

The properties worth pinning are the ones an operator cannot see from
the response: that a same-named upload *replaces* instead of failing,
that the displaced bundle key survives in the audit payload, and that
uploading is not publishing.

OSS is stubbed — the object store is not what these assert. Bundle
parsing is real, so the fixtures are real zips.
"""

import io
import zipfile
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import skill as admin_skill
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.admin_audit_log import AdminAuditLog
from app.models.skill import Skill, SkillOrigin, SkillStatus
from app.models.staff import Staff, StaffStatus

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _bundle(name: str = "smoke-skill") -> bytes:
    """A minimal valid bundle: SKILL.md with the front matter the parser
    requires, at the zip root."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: a seeded skill\n---\n\nBody.\n",
        )
    return buffer.getvalue()


class _FakeDb:
    """``scalar`` serves the by-name platform lookup; everything the
    endpoint writes lands in ``added``."""

    def __init__(self, existing: Skill | None = None) -> None:
        self.executed: list = []
        self.existing = existing
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return self.existing

    def add(self, obj) -> None:
        self.added.append(obj)
        if isinstance(obj, Skill):
            obj.id = 5
            obj.created_at = obj.updated_at = _NOW

    async def execute(self, stmt):
        self.executed.append(stmt)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def _existing_skill() -> Skill:
    skill = Skill(
        id=5,
        user_id=None,
        origin=SkillOrigin.OFFICIAL,
        name="smoke-skill",
        display_name="Old Name",
        description="old",
        status=SkillStatus.PUBLISHED,
        bundle_key="official/smoke-skill/OLD/smoke-skill.zip",
        bundle_size=10,
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


def _post(client: TestClient, data: bytes, origin: str = "OFFICIAL", **form):
    return client.post(
        "/skills",
        files={"file": ("smoke-skill.zip", data, "application/zip")},
        data={"origin": origin, **form},
    )


@pytest.fixture(autouse=True)
def oss_keys(monkeypatch):
    """Record bundle keys instead of writing to the bucket."""
    keys: list[str] = []
    monkeypatch.setattr(
        "app.services.skill_registration.file_storage.upload",
        lambda _policy, key, _data: keys.append(key),
    )
    return keys


def _audit_rows(db: _FakeDb) -> list[AdminAuditLog]:
    return [obj for obj in db.added if isinstance(obj, AdminAuditLog)]


def test_a_new_bundle_lands_staged_and_audited(oss_keys) -> None:
    db = _FakeDb()

    response = _post(_client(db), _bundle())

    assert response.status_code == 201
    body = response.json()
    assert body["origin"] == "OFFICIAL"
    assert body["name"] == "smoke-skill"
    assert body["user_id"] is None and body["owner_username"] is None
    # Uploading is not publishing: the one thing an upload cannot undo
    # is having been visible, so going live is a separate act.
    assert body["status"] == "STAGED"

    (row,) = _audit_rows(db)
    assert row.action == "skill.upload"
    assert (row.target_type, row.target_id) == ("skill", "5")
    assert row.payload["name"] == "smoke-skill"
    assert row.payload["replaced_bundle_key"] is None
    assert row.payload["bundle_key"] == oss_keys[0]
    assert db.commits == 1


def test_a_system_bundle_stages_too() -> None:
    """``origin`` decides who may see a skill, never whether it is live."""
    db = _FakeDb()

    response = _post(_client(db), _bundle(), origin="SYSTEM")

    assert response.status_code == 201
    assert response.json()["status"] == "STAGED"


def test_a_replace_leaves_a_live_listing_live() -> None:
    """Shipping a fixed bundle is a content change, not a policy one.

    Nothing in the write touches ``status``, so an operator shipping a
    fix cannot pull the skill out from under the users who have it —
    which is what re-deriving from the origin would have done.
    """
    live = _existing_skill()
    db = _FakeDb(existing=live)

    response = _post(_client(db), _bundle(), origin="OFFICIAL")

    assert response.status_code == 201
    assert live.status is SkillStatus.PUBLISHED
    assert response.json()["status"] == "PUBLISHED"


def test_a_replace_leaves_a_staged_listing_staged() -> None:
    """The rule is "leave it alone", not "leave it live"."""
    staged = _existing_skill()
    staged.status = SkillStatus.STAGED
    db = _FakeDb(existing=staged)

    _post(_client(db), _bundle(), origin="SYSTEM")

    assert staged.status is SkillStatus.STAGED


def test_the_same_name_replaces_and_records_the_displaced_bundle() -> None:
    existing = _existing_skill()
    db = _FakeDb(existing=existing)

    response = _post(_client(db), _bundle(), origin="SYSTEM")

    assert response.status_code == 201
    # Updated in place: the id users' preferences point at is unchanged,
    # and no second row was inserted.
    assert response.json()["id"] == 5
    assert [obj for obj in db.added if isinstance(obj, Skill)] == []
    assert existing.origin is SkillOrigin.SYSTEM
    assert existing.bundle_key != "official/smoke-skill/OLD/smoke-skill.zip"

    (row,) = _audit_rows(db)
    # The only surviving pointer to the version this displaced.
    assert row.payload["replaced_bundle_key"] == "official/smoke-skill/OLD/smoke-skill.zip"


def test_a_bundle_without_skill_md_is_rejected_and_writes_nothing() -> None:
    db = _FakeDb()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.md", "no front matter here")

    response = _post(_client(db), buffer.getvalue())

    assert response.status_code == 400
    assert db.added == [] and db.commits == 0


def test_a_custom_origin_is_not_accepted() -> None:
    """``CUSTOM`` belongs to a user; the console has no such upload."""
    db = _FakeDb()

    response = _post(_client(db), _bundle(), origin="CUSTOM")

    assert response.status_code == 422
    assert db.added == [] and db.commits == 0


def test_turning_a_listing_agent_only_drops_what_users_said_about_it() -> None:
    """Nothing lists a SYSTEM skill, so nothing could show or change a
    preference row left behind — keeping one would break the invariant
    that a preference row only exists for a skill its user can see.
    """
    live = _existing_skill()  # OFFICIAL
    db = _FakeDb(existing=live)

    _post(_client(db), _bundle(), origin="SYSTEM")

    deletes = [str(s) for s in db.executed if str(s).startswith("DELETE")]
    assert len(deletes) == 1
    assert "user_skill_preference" in deletes[0]


def test_replacing_within_the_same_origin_leaves_preferences_alone() -> None:
    """The drop is for the origin change, not for every upload."""
    db = _FakeDb(existing=_existing_skill())  # OFFICIAL

    _post(_client(db), _bundle(), origin="OFFICIAL")

    assert not [s for s in db.executed if str(s).startswith("DELETE")]
