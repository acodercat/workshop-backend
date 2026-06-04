"""Registration-level tests for skill uploads — DB and OSS are mocked,
so these exercise ``register_skill_bundle``'s dispatch/normalization
without touching Postgres or object storage. Bundle *parsing* itself is
covered by ``test_skill_bundle.py``.
"""

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock

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
