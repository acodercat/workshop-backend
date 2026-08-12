from uuid import uuid4

import pytest

from app.models.session_files import SessionFile
from app.schemas.session import FileRef
from app.services import file_storage
from app.services.session.enqueue import _bind_session_files
from app.services.session.exceptions import (
    FileNotInSessionError,
    FileNotUploadedError,
)


class _Rows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows=()) -> None:
        self.rows = rows
        self.added = []
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Rows(self.rows)

    def add(self, obj) -> None:
        self.added.append(obj)


def _file(file_key: str) -> FileRef:
    # ``size_bytes`` here is the *client-declared* value; the binder no
    # longer trusts it — the real size comes from ``head_size`` (stubbed).
    return FileRef(file_key=file_key, filename="report.pdf", size_bytes=1)


@pytest.fixture
def stub_head_size(monkeypatch):
    """Stub the blocking OSS ``head_size`` with a configurable real size.
    Default 100 bytes; raise ``FileObjectNotFoundError`` for keys listed
    in ``missing``."""

    state = {"size": 100, "missing": set()}

    def _fake(policy, file_key):
        if file_key in state["missing"]:
            raise file_storage.FileObjectNotFoundError(file_key)
        return state["size"]

    monkeypatch.setattr(file_storage, "head_size", _fake)
    return state


async def test_bind_session_files_inserts_each_new_key_once(stub_head_size) -> None:
    session_id = uuid4()
    db = _Db()

    newly_bound = await _bind_session_files(
        db,
        session_id=session_id,
        user_id=7,
        files=[_file("documents/a"), _file("documents/a"), _file("documents/b")],
    )

    assert [obj.file_key for obj in db.added] == ["documents/a", "documents/b"]
    assert all(isinstance(obj, SessionFile) for obj in db.added)
    assert all(obj.session_id == session_id for obj in db.added)
    # The owner is denormalized onto every row from the session.
    assert all(obj.user_id == 7 for obj in db.added)
    # Only freshly-bound keys are returned (for orphan-tag clearing, M5).
    assert newly_bound == ["documents/a", "documents/b"]


async def test_bind_persists_real_oss_size_not_client_declared(
    stub_head_size,
) -> None:
    # The client declared size_bytes=1 (via _file); the row must carry the
    # true OSS size (100), so the per-user quota SUM can't be under-counted.
    stub_head_size["size"] = 100
    db = _Db()

    await _bind_session_files(
        db,
        session_id=uuid4(),
        user_id=7,
        files=[_file("documents/a")],
    )

    assert db.added[0].size_bytes == 100


async def test_bind_rejects_object_over_per_file_cap(stub_head_size) -> None:
    stub_head_size["size"] = file_storage.SESSION_FILE_POLICY.max_bytes + 1
    db = _Db()

    with pytest.raises(file_storage.FileTooLargeError):
        await _bind_session_files(
            db,
            session_id=uuid4(),
            user_id=7,
            files=[_file("documents/big")],
        )

    assert db.added == []  # nothing bound when a file overflows


async def test_bind_rejects_never_uploaded_object(stub_head_size) -> None:
    stub_head_size["missing"] = {"documents/ghost"}
    db = _Db()

    with pytest.raises(FileNotUploadedError, match="was not uploaded"):
        await _bind_session_files(
            db,
            session_id=uuid4(),
            user_id=7,
            files=[_file("documents/ghost")],
        )

    assert db.added == []


async def test_bind_session_files_skips_key_already_bound_to_same_session(
    stub_head_size,
) -> None:
    session_id = uuid4()
    db = _Db(rows=[("documents/a", session_id)])

    newly_bound = await _bind_session_files(
        db,
        session_id=session_id,
        user_id=7,
        files=[_file("documents/a")],
    )

    assert db.added == []
    # Already bound — not returned, so its tag isn't re-cleared.
    assert newly_bound == []


async def test_bind_session_files_rejects_key_bound_to_another_session(
    stub_head_size,
) -> None:
    session_id = uuid4()
    other_session_id = uuid4()
    db = _Db(rows=[("documents/a", other_session_id)])

    with pytest.raises(FileNotInSessionError, match="already bound"):
        await _bind_session_files(
            db,
            session_id=session_id,
            user_id=7,
            files=[_file("documents/a")],
        )

    assert db.added == []
