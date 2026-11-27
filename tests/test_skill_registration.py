"""Registration-level tests for skill uploads — DB and OSS are mocked,
so these exercise ``register_skill_bundle``'s dispatch/normalization
without touching Postgres or object storage. Bundle *parsing* itself is
covered by ``test_skill_bundle.py``.
"""

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.skill import Skill, SkillOrigin, SkillStatus
from app.services import skill_registration


def _skill_zip(name: str = "renamed") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", f"---\nname: {name}\n---\nbody")
    return buf.getvalue()


async def test_dot_skill_upload_parsed_as_zip_and_stored_as_zip(monkeypatch):
    """A ``.skill`` file is a renamed zip: it must be accepted, parsed via
    the zip path, and stored under a ``.zip`` key — the worker's staging
    dispatches on the ``.zip`` suffix, so a ``.skill`` key would break it."""
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        skill_registration.file_storage,
        "upload",
        lambda policy, key, data: captured.update(key=key),
    )
    db = MagicMock()
    db.scalar = AsyncMock(return_value=0)  # under the per-user quota
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    skill = await skill_registration.register_skill_bundle(
        db, user_id=1, filename="My-Skill.skill", data=_skill_zip("renamed")
    )

    assert skill.name == "renamed"  # parsed via the zip path, not as bare md
    assert captured["key"].endswith("/renamed.zip")  # canonicalized to .zip


class _FlushFails:
    """A session whose flush fails. It has no ``rollback``: the transaction
    is the caller's, so the service reaching for one fails the test."""

    def add(self, row: object) -> None:
        pass

    async def scalar(self, statement: object) -> None:
        return None  # the platform name is free

    async def flush(self) -> None:
        raise RuntimeError("flush failed")


async def test_a_platform_flush_failure_removes_the_upload_and_leaves_the_rollback(monkeypatch):
    uploaded: list[str] = []
    deleted: list[str] = []
    monkeypatch.setattr(
        skill_registration.file_storage, "upload", lambda policy, key, data: uploaded.append(key)
    )
    monkeypatch.setattr(
        skill_registration, "_best_effort_delete_oss", AsyncMock(side_effect=deleted.append)
    )

    with pytest.raises(RuntimeError, match="flush failed"):
        await skill_registration.register_platform_skill_bundle(
            _FlushFails(), origin=SkillOrigin.OFFICIAL, filename="pdf.zip", data=_skill_zip("pdf")
        )

    assert uploaded and deleted == uploaded


async def test_keep_listing_keeps_a_curated_listing_over_the_bundles(monkeypatch):
    """New bytes under a listing an operator curated: the bundle supplies
    the object, the named row supplies what users read."""
    monkeypatch.setattr(skill_registration.file_storage, "upload", lambda policy, key, data: None)
    existing = Skill(
        user_id=None,
        origin=SkillOrigin.OFFICIAL,
        name="pdf",
        display_name="PDF toolkit",
        display_name_i18n={"zh": "PDF 工具"},
        description="Curated by an operator.",
        bundle_key="official/pdf/old/pdf.zip",
        bundle_size=1,
        status=SkillStatus.PUBLISHED,
    )
    db = MagicMock()
    db.scalar = AsyncMock(return_value=existing)
    db.flush = AsyncMock()

    skill, replaced_key = await skill_registration.register_platform_skill_bundle(
        db,
        origin=SkillOrigin.OFFICIAL,
        filename="pdf.zip",
        data=_skill_zip("pdf"),
        keep_listing=True,
    )

    assert skill is existing
    assert replaced_key == "official/pdf/old/pdf.zip"
    assert skill.bundle_key != replaced_key
    assert (skill.display_name, skill.display_name_i18n, skill.description) == (
        "PDF toolkit",
        {"zh": "PDF 工具"},
        "Curated by an operator.",
    )
    assert skill.status is SkillStatus.PUBLISHED
