from uuid import uuid4

import pytest

from app.models.session_files import SessionFile
from app.schemas.session import FileRef
from app.services.session.enqueue import _bind_session_files
from app.services.session.exceptions import FileNotInSessionError


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
    return FileRef(file_key=file_key, filename="report.pdf", size_bytes=1)


async def test_bind_session_files_inserts_each_new_key_once() -> None:
    session_id = uuid4()
    db = _Db()

    newly_bound = await _bind_session_files(
        db,
        session_id=session_id,
        files=[_file("documents/a"), _file("documents/a"), _file("documents/b")],
    )

    assert [obj.file_key for obj in db.added] == ["documents/a", "documents/b"]
    assert all(isinstance(obj, SessionFile) for obj in db.added)
    assert all(obj.session_id == session_id for obj in db.added)
    # Only freshly-bound keys are returned (for orphan-tag clearing, M5).
    assert newly_bound == ["documents/a", "documents/b"]


async def test_bind_session_files_skips_key_already_bound_to_same_session() -> None:
    session_id = uuid4()
    db = _Db(rows=[("documents/a", session_id)])

    newly_bound = await _bind_session_files(
        db,
        session_id=session_id,
        files=[_file("documents/a")],
    )

    assert db.added == []
    # Already bound — not returned, so its tag isn't re-cleared.
    assert newly_bound == []


async def test_bind_session_files_rejects_key_bound_to_another_session() -> None:
    session_id = uuid4()
    other_session_id = uuid4()
    db = _Db(rows=[("documents/a", other_session_id)])

    with pytest.raises(FileNotInSessionError, match="already bound"):
        await _bind_session_files(
            db,
            session_id=session_id,
            files=[_file("documents/a")],
        )

    assert db.added == []
