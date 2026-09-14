"""A saved attachment is one object storage has agreed to keep.

Uploads are born tagged for the bucket's ~1-day orphan reaper. A message's
attachment is read within the turn that binds it, so clearing the tag there
may fail quietly; a scheduled task may not fire for days, so an attachment
whose tag survived its save is deleted underneath a task that reports it
saved. These pin the scheduled side: cleared before commit, for every kept
attachment, or the save is refused.
"""

from types import SimpleNamespace

import pytest

from app.errors import FileRetentionFailedError
from app.models.schedule_file import ScheduleFile
from app.schemas.session import FileRef
from app.services import file_storage
from app.services.schedule import files as schedule_files


def _ref(key: str) -> FileRef:
    return FileRef(file_key=key, filename=f"{key}.pdf", size_bytes=1)


class _Db:
    def __init__(self) -> None:
        self.added: list[ScheduleFile] = []
        self.deleted: list[object] = []

    def add(self, row: ScheduleFile) -> None:
        self.added.append(row)

    async def delete(self, row: object) -> None:
        self.deleted.append(row)


@pytest.fixture
def storage(monkeypatch):
    """Object storage as the attachment service sees it."""
    state = {"cleared": [], "refuse": set(), "sized": []}

    async def owned(_db, *, user_id, file_keys):
        return None

    async def sizes(_policy, keys):
        state["sized"].append(list(keys))
        return [100 for _ in keys]

    def clear(_policy, key):
        state["cleared"].append(key)
        if key in state["refuse"]:
            raise OSError("storage refused")

    monkeypatch.setattr(schedule_files, "assert_files_owned_by", owned)
    monkeypatch.setattr(file_storage, "confirmed_sizes", sizes)
    monkeypatch.setattr(file_storage, "clear_orphan_tag", clear)
    monkeypatch.setattr(file_storage.settings, "oss_orphan_tagging_enabled", True)
    return state


def _stored(monkeypatch, *keys: str) -> None:
    async def attached(_db, _schedule_id):
        return [SimpleNamespace(file_key=key) for key in keys]

    monkeypatch.setattr(schedule_files, "attached", attached)


class TestReplace:
    async def test_every_kept_attachment_is_cleared_not_only_the_new_one(
        self, monkeypatch, storage
    ):
        # Resaving is the remedy a refused save points its owner at, so it
        # has to repair what an earlier save could not clear.
        _stored(monkeypatch, "old")
        db = _Db()

        await schedule_files.replace(db, 1, 7, [_ref("old"), _ref("new")])

        assert sorted(storage["cleared"]) == ["new", "old"]
        # Only the new one is sized and inserted; the kept row stands.
        assert storage["sized"] == [["new"]]
        assert [row.file_key for row in db.added] == ["new"]

    async def test_a_refusal_fails_the_save_before_anything_is_stored(self, monkeypatch, storage):
        _stored(monkeypatch)
        storage["refuse"] = {"b"}
        db = _Db()

        with pytest.raises(FileRetentionFailedError):
            await schedule_files.replace(db, 1, 7, [_ref("a"), _ref("b")])

        assert db.added == []
        # Every key was still attempted, so the retry has less left to do.
        assert sorted(storage["cleared"]) == ["a", "b"]

    async def test_a_removed_attachment_is_not_cleared_again(self, monkeypatch, storage):
        _stored(monkeypatch, "gone", "kept")
        db = _Db()

        await schedule_files.replace(db, 1, 7, [_ref("kept")])

        assert storage["cleared"] == ["kept"]
        assert [row.file_key for row in db.deleted] == ["gone"]


class TestClearOrphanTags:
    async def test_nothing_to_clear_when_uploads_are_never_tagged(self, monkeypatch, storage):
        monkeypatch.setattr(file_storage.settings, "oss_orphan_tagging_enabled", False)

        await file_storage.clear_orphan_tags(file_storage.SESSION_FILE_POLICY, ["a"])

        assert storage["cleared"] == []

    async def test_a_failure_is_raised_for_the_caller_to_judge(self, storage):
        storage["refuse"] = {"a"}

        with pytest.raises(FileRetentionFailedError, match="1 of 2"):
            await file_storage.clear_orphan_tags(file_storage.SESSION_FILE_POLICY, ["a", "b"])

    def test_the_refusal_is_a_retryable_upstream_error(self):
        exc = FileRetentionFailedError("x")
        assert exc.status_code == 502
        assert exc.error_type == "file_retention_failed"
