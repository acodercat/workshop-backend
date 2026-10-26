"""Deleting a CUSTOM skill keeps the bundle a live turn has pinned.

A pick snapshots ``bundle_key`` at enqueue and the worker stages the bundle
only when the model invokes the skill, so removing the object while that
turn is queued or running would fail it.
"""

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

from app.api.public import skill as endpoint


async def _delete(monkeypatch, *, pinned: bool) -> MagicMock:
    skill = MagicMock(bundle_key="custom/7/x/u/x.zip")
    skill.name = "x"
    monkeypatch.setattr(endpoint, "_get_owned_skill", AsyncMock(return_value=skill))
    monkeypatch.setattr(endpoint, "_pinned_by_a_live_turn", AsyncMock(return_value=pinned))
    delete = MagicMock()
    monkeypatch.setattr(endpoint.file_storage, "delete", delete)

    await endpoint.delete_skill(1, db=AsyncMock(), current_user=MagicMock(id=7))
    return delete


async def test_a_pinned_bundle_is_kept(monkeypatch):
    delete = await _delete(monkeypatch, pinned=True)

    delete.assert_not_called()


async def test_an_unpinned_bundle_is_removed(monkeypatch):
    delete = await _delete(monkeypatch, pinned=False)

    delete.assert_called_once_with(endpoint.SKILL_POLICY, "custom/7/x/u/x.zip")


async def test_only_live_turns_count_as_pinning():
    db = AsyncMock()
    db.scalar.return_value = False

    await endpoint._pinned_by_a_live_turn(db, "custom/7/x/u/x.zip")

    sql = str(db.scalar.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "session_turn.status IN" in sql
    assert "session_turn.skills @>" in sql
