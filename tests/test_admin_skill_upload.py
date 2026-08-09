"""``POST /skills`` — the console's platform-bundle upload.

The properties worth pinning are the ones an operator cannot see from
the response: that a same-named upload *replaces* instead of failing,
that the displaced bundle key survives in the audit payload, and that
``enabled`` follows the origin rather than the uploader's intent.

OSS is stubbed — the object store is not what these assert. Bundle
parsing is real, so the fixtures are real zips.
"""

import io
import zipfile
from datetime import datetime, timezone

import pytest
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

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def _existing_skill() -> Skill:
    skill = Skill(
        id=5, user_id=None, origin=SkillOrigin.OFFICIAL, name="smoke-skill",
        display_name="Old Name", description="old", enabled=True,
        bundle_key="official/smoke-skill/OLD/smoke-skill.zip", bundle_size=10,
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


def test_a_new_official_bundle_lands_disabled_and_audited(oss_keys) -> None:
    db = _FakeDb()

    response = _post(_client(db), _bundle())

    assert response.status_code == 201
    body = response.json()
    assert body["origin"] == "OFFICIAL"
    assert body["name"] == "smoke-skill"
    assert body["user_id"] is None and body["owner_username"] is None
    # OFFICIAL is every user's default, so it must not arrive switched on.
    assert body["enabled"] is False

    (row,) = _audit_rows(db)
    assert row.action == "skill.upload"
    assert (row.target_type, row.target_id) == ("skill", "5")
    assert row.payload["name"] == "smoke-skill"
    assert row.payload["replaced_bundle_key"] is None
    assert row.payload["bundle_key"] == oss_keys[0]
    assert db.commits == 1


def test_a_system_bundle_lands_enabled() -> None:
    db = _FakeDb()

    response = _post(_client(db), _bundle(), origin="SYSTEM")

    assert response.status_code == 201
    assert response.json()["enabled"] is True


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
    assert (
        row.payload["replaced_bundle_key"]
        == "official/smoke-skill/OLD/smoke-skill.zip"
    )


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


def test_the_audit_row_carries_no_reason() -> None:
    """A create records itself: the payload is the account of the deed,
    so the console does not ask for a typed justification."""
    db = _FakeDb()

    _post(_client(db), _bundle())

    (row,) = _audit_rows(db)
    assert row.reason is None
