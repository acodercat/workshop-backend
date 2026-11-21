from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app.errors import FileNotOwnedError, FileNotUploadedError
from app.models.session_files import SessionFile
from app.schemas.session import FileRef
from app.services import file_storage
from app.services.session import enqueue
from app.services.session.enqueue import _bind_session_files
from app.services.session.files import assert_files_owned_by


class _Rows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return iter(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Db:
    """Answers the binder's one remaining query: keys already bound here.

    Ownership is no longer the binder's own query — it calls
    ``assert_files_owned_by``, which the ``owned`` fixture stubs and
    ``TestOwnership`` below covers against real SQL.
    """

    def __init__(self, bound_here=()) -> None:
        self.bound_here = list(bound_here)
        self.added = []

    async def execute(self, statement):
        return _Rows(self.bound_here)

    def add(self, obj) -> None:
        self.added.append(obj)


class _CapturingDb:
    """Records the one statement it is given, compiled with literal binds."""

    sql = ""

    async def execute(self, statement):
        self.sql = str(
            statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        return _Rows([])


@pytest.fixture(autouse=True)
def owned(monkeypatch):
    """Every key belongs to the caller unless a test says otherwise."""
    state = {"foreign": set()}

    async def _fake(_db, *, user_id, file_keys):
        for key in file_keys:
            if key in state["foreign"]:
                raise FileNotOwnedError(f"file_key {key!r} belongs to another user")

    monkeypatch.setattr(enqueue, "assert_files_owned_by", _fake)
    return state


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
    db = _Db(bound_here=["documents/a"])

    newly_bound = await _bind_session_files(
        db,
        session_id=uuid4(),
        user_id=7,
        files=[_file("documents/a")],
    )

    assert db.added == []
    # Already bound — not returned, so its tag isn't re-cleared.
    assert newly_bound == []


async def test_bind_session_files_lets_an_owner_rebind_in_another_session(
    stub_head_size,
) -> None:
    """An owner may reuse an upload in another of their sessions.

    The key being bound elsewhere is invisible to this session's lookup, so
    it inserts.
    """
    session_id = uuid4()
    db = _Db(bound_here=[])

    newly_bound = await _bind_session_files(
        db,
        session_id=session_id,
        user_id=7,
        files=[_file("documents/a")],
    )

    assert [row.session_id for row in db.added] == [session_id]
    assert newly_bound == ["documents/a"]


async def test_bind_session_files_rejects_a_key_owned_by_someone_else(
    stub_head_size, owned
) -> None:
    # Ownership is checked before anything is written, and before the OSS
    # HEAD is spent on a key the caller could never have bound.
    owned["foreign"] = {"documents/a"}
    db = _Db()

    with pytest.raises(FileNotOwnedError, match="another user"):
        await _bind_session_files(
            db,
            session_id=uuid4(),
            user_id=7,
            files=[_file("documents/a")],
        )

    assert db.added == []


class TestOwnership:
    """``assert_files_owned_by`` — the predicate every storing path shares.

    Asserted on the compiled statement, not a stub's return: the earlier
    tests here stubbed the query away, and mutating the predicate's table,
    column or operator left every one of them green.
    """

    async def _compiled(self, user_id, file_keys) -> str:
        db = _CapturingDb()
        await assert_files_owned_by(db, user_id=user_id, file_keys=file_keys)
        return db.sql

    async def test_checks_every_table_that_can_hold_a_key(self):
        # A project file uploaded on the project's page may never reach
        # ``session_files`` at all. Reading that table alone would leave it
        # unheld, and a stranger with the key could bind it first.
        sql = await self._compiled(7, ["k"])
        assert "FROM session_files" in sql
        assert "FROM project_file" in sql
        assert "UNION ALL" in sql

    async def test_a_null_owner_fails_closed_for_a_logged_in_caller(self):
        # The one thing ``IS DISTINCT FROM`` buys over ``!=``: a row whose
        # owner is NULL is *unknown* to ``!=`` and silently dropped.
        sql = await self._compiled(7, ["k"])
        assert sql.count("user_id IS DISTINCT FROM 7") == 2
        assert "!= 7" not in sql

    async def test_a_deleted_project_file_still_holds_its_key(self):
        # The object is still its owner's after the file or its project is
        # deleted, as a soft-deleted session's uploads are.
        assert "deleted_at" not in await self._compiled(7, ["k"])

    async def test_only_the_requested_keys_are_consulted(self):
        sql = await self._compiled(7, ["a", "b"])
        assert sql.count("file_key IN ('a', 'b')") == 2

    async def test_any_foreign_row_makes_the_key_foreign(self):
        class _Foreign:
            async def execute(self, statement):
                return _Rows(["documents/a"])

        with pytest.raises(FileNotOwnedError, match="another user"):
            await assert_files_owned_by(_Foreign(), user_id=7, file_keys=["documents/a"])

    async def test_a_key_nobody_holds_passes(self):
        # Only its minter can know it; refusing it would refuse every upload.
        class _Unheld:
            async def execute(self, statement):
                return _Rows([])

        await assert_files_owned_by(_Unheld(), user_id=7, file_keys=["fresh"])

    async def test_no_keys_is_not_a_roundtrip(self):
        class _Never:
            async def execute(self, statement):
                raise AssertionError("queried with nothing to check")

        await assert_files_owned_by(_Never(), user_id=7, file_keys=[])


async def test_files_are_bound_before_the_user_row_is_locked(run_enqueue) -> None:
    # Binding HEADs each new object in OSS. Under the reservation's user-row
    # lock, that round trip would hold up every other write for the user.
    steps = await run_enqueue(AsyncMock())

    assert steps.index("_bind_session_files") < steps.index("reserve_for_turn")


class TestSchema:
    def test_one_object_may_be_bound_to_several_sessions(self):
        # An owner may reuse an upload in another of their sessions; the old
        # column-level UNIQUE on ``file_key`` refused that with a 403.
        column = SessionFile.__table__.c.file_key  # type: ignore[attr-defined]
        assert not column.unique
        uniques = {
            tuple(column.name for column in constraint.columns)
            for constraint in SessionFile.__table__.constraints  # type: ignore[attr-defined]
            if isinstance(constraint, UniqueConstraint)
        }
        assert ("session_id", "file_key") in uniques

    def test_the_owner_lookup_has_an_index(self):
        # ``_bind_session_files`` looks keys up with no session filter to
        # find who holds them; the composite unique cannot serve that.
        indexes = {
            item.name
            for item in SessionFile.__table__.indexes  # type: ignore[attr-defined]
            if isinstance(item, Index)
        }
        assert "ix_session_files_file_key" in indexes
