"""The console can pull back a platform bundle, and only a platform one.

Being able to fetch the bytes is what turns the upload audit row's
``replaced_bundle_key`` into a rollback rather than a note. A ``CUSTOM``
upload is refused: it is a user's private file, the same class of thing
as a conversation, which this console removed rather than audited.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import skill as admin_skill
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.skill import Skill, SkillOrigin
from app.models.staff import Staff, StaffStatus

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeDb:
    """``get`` serves the PK lookup; nothing else is reached."""

    def __init__(self, skill: Skill | None) -> None:
        self.skill = skill

    async def get(self, _model, _pk):
        return self.skill


def _skill(**over) -> Skill:
    skill = Skill(
        **{
            "id": 5,
            "user_id": None,
            "origin": SkillOrigin.OFFICIAL,
            "name": "pdf-tools",
            "display_name": "PDF",
            "description": "reads pdfs",
            "enabled": True,
            "bundle_key": "official/pdf/9f3c1a2b/original-name.zip",
            "bundle_size": 10,
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


def test_the_url_is_signed_for_the_slug_not_the_stored_key(monkeypatch) -> None:
    """The OSS key carries a UUID and whatever the uploader called the
    file; neither means anything to the operator opening it. The saved
    name is the skill's own identifier plus the key's extension — the
    same choice the customer surface makes, so a bundle saved from
    either lands under one name."""
    signed: dict = {}

    def fake_url(policy, file_key, filename):
        signed.update(policy=policy, file_key=file_key, filename=filename)
        return "https://oss.example/signed", 900

    monkeypatch.setattr(admin_skill.file_storage, "generate_download_url", fake_url)

    body = _client(_FakeDb(_skill())).get("/skills/5/download-url").json()

    assert signed["file_key"] == "official/pdf/9f3c1a2b/original-name.zip"
    assert signed["filename"] == "pdf-tools.zip"
    assert body == {
        "download_url": "https://oss.example/signed",
        "file_key": "official/pdf/9f3c1a2b/original-name.zip",
        "expires_in": 900,
    }


def test_a_users_own_upload_is_refused(monkeypatch) -> None:
    """403, not 404: the row is on the operator's screen, so denying its
    existence would read as a bug. What is refused is the bytes."""

    def fail(*_args, **_kwargs):
        raise AssertionError("no URL should be signed for a CUSTOM bundle")

    monkeypatch.setattr(admin_skill.file_storage, "generate_download_url", fail)

    response = _client(_FakeDb(_skill(origin=SkillOrigin.CUSTOM, user_id=42))).get(
        "/skills/5/download-url"
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "private_skill_bundle"


def test_a_system_bundle_is_downloadable(monkeypatch) -> None:
    """``SYSTEM`` skills are hidden from users entirely, which is a
    reason the console can reach them rather than a reason it cannot:
    they are the platform's own artifact and nobody else's. The customer
    surface cannot serve these at all, so this is the only way to read
    one back."""
    monkeypatch.setattr(
        admin_skill.file_storage,
        "generate_download_url",
        lambda *_args: ("https://oss.example/signed", 900),
    )

    response = _client(_FakeDb(_skill(origin=SkillOrigin.SYSTEM))).get("/skills/5/download-url")

    assert response.status_code == 200
    assert response.json()["download_url"] == "https://oss.example/signed"


def test_a_key_with_no_extension_still_saves_as_a_zip() -> None:
    """The fallback branch of ``Skill.download_filename``. Bundles are
    zips, so a key that lost its suffix somewhere should not hand the
    operator an extensionless file the OS cannot open."""
    assert _skill(bundle_key="official/pdf/9f3c1a2b/blob").download_filename == ("pdf-tools.zip")


def test_the_boundary_is_an_allowlist() -> None:
    """A privacy check written as ``!= CUSTOM`` makes any origin added
    later downloadable the moment it exists. Pinned as the set it is, so
    a new member has to be put here on purpose."""
    assert admin_skill._DOWNLOADABLE_ORIGINS == {
        SkillOrigin.SYSTEM,
        SkillOrigin.OFFICIAL,
    }
    assert set(SkillOrigin) - admin_skill._DOWNLOADABLE_ORIGINS == {SkillOrigin.CUSTOM}


def test_an_unknown_id_is_a_404() -> None:
    response = _client(_FakeDb(None)).get("/skills/5/download-url")

    assert response.status_code == 404
