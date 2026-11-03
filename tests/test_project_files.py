"""A project's files (``app/services/project/files.py``).

No DB: the rules worth pinning are in the statements' predicates and in the
order ``add`` takes its steps, so the queries are compiled and the steps
around them are stubbed.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.errors import FileRetentionFailedError, ProjectFileLimitError
from app.models.project import Project
from app.models.project_file import ProjectFile, ProjectFileOrigin
from app.schemas.project import ProjectFileCreate
from app.services import file_storage
from app.services.project import files as project_files

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
SESSION_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _sql(statement) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


class _Db:
    """Answers ``scalar`` from a queue and records every statement."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.statements: list[str] = []
        self.added: list[ProjectFile] = []

    async def scalar(self, statement):
        self.statements.append(_sql(statement))
        return self.answers.pop(0)

    async def execute(self, statement):
        self.statements.append(_sql(statement))

    def add(self, row: ProjectFile) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


def _project() -> Project:
    return Project(id=PROJECT_ID, user_id=7)


def _upload(filename: str = "report.pdf") -> ProjectFileCreate:
    return ProjectFileCreate(upload={"file_key": "documents/u/report.pdf", "filename": filename})


class TestLatest:
    def test_each_live_names_highest_version(self) -> None:
        sql = _sql(project_files.latest(PROJECT_ID))
        assert "DISTINCT ON (project_file.filename)" in sql
        assert "ORDER BY project_file.filename, project_file.version DESC" in sql
        assert "project_file.deleted_at IS NULL" in sql


class TestNextVersion:
    async def test_numbers_on_from_the_highest_ever_written(self) -> None:
        db = _Db(3)
        assert await project_files._next_version(db, PROJECT_ID, "a.md") == 4
        # Deleted versions included, so the unique constraint never collides.
        assert "deleted_at" not in db.statements[0]

    async def test_a_new_name_starts_at_one(self) -> None:
        assert await project_files._next_version(_Db(None), PROJECT_ID, "a.md") == 1


class TestHeadroom:
    async def test_a_new_version_is_never_refused(self) -> None:
        db = _Db(True)
        await project_files._assert_headroom(db, PROJECT_ID, "a.md")
        assert len(db.statements) == 1

    async def test_a_new_name_at_the_cap_is_refused(self) -> None:
        with pytest.raises(ProjectFileLimitError):
            await project_files._assert_headroom(
                _Db(False, project_files._MAX_FILES_PER_PROJECT), PROJECT_ID, "a.md"
            )

    async def test_a_new_name_below_the_cap_passes(self) -> None:
        await project_files._assert_headroom(
            _Db(False, project_files._MAX_FILES_PER_PROJECT - 1), PROJECT_ID, "a.md"
        )


class TestRemove:
    async def test_every_live_version_of_the_name_goes(self) -> None:
        db = _Db()
        file = ProjectFile(id=5, project_id=PROJECT_ID, filename="a.md")
        await project_files.remove(db, _project(), file)
        (sql,) = db.statements
        assert "UPDATE project_file SET deleted_at=now()" in sql
        assert "project_file.filename = 'a.md'" in sql
        assert "project_file.id" not in sql


@pytest.fixture
def steps(monkeypatch):
    """Everything ``add`` calls around its own logic, recording the order."""
    order: list[str] = []
    state = SimpleNamespace(order=order, refuse_clear=False, at_cap=False)

    async def owned(_db, *, user_id, file_keys):
        order.append("owned")

    async def headroom(_db, _project_id, _filename):
        order.append("headroom")
        if state.at_cap:
            raise ProjectFileLimitError(project_files._MAX_FILES_PER_PROJECT)

    async def sizes(_policy, keys):
        order.append("sized")
        return [123 for _ in keys]

    async def clear(_policy, keys):
        order.append("cleared")
        if state.refuse_clear:
            raise FileRetentionFailedError("storage refused")

    async def next_version(_db, _project_id, _filename):
        return 2

    monkeypatch.setattr(project_files, "assert_files_owned_by", owned)
    monkeypatch.setattr(project_files, "_assert_headroom", headroom)
    monkeypatch.setattr(file_storage, "confirmed_sizes", sizes)
    monkeypatch.setattr(file_storage, "clear_orphan_tags", clear)
    monkeypatch.setattr(project_files, "_next_version", next_version)
    return state


class TestAdd:
    async def test_an_upload_is_checked_sized_and_kept_then_stored(self, steps) -> None:
        db = _Db()
        file = await project_files.add(db, _project(), _upload())

        assert steps.order == ["owned", "headroom", "sized", "cleared"]
        assert db.added == [file]
        assert file.origin is ProjectFileOrigin.UPLOAD
        assert file.origin_session_id is None
        assert (file.project_id, file.user_id) == (PROJECT_ID, 7)
        # OSS's size, never the client's.
        assert (file.size_bytes, file.version) == (123, 2)

    async def test_a_refusal_comes_before_the_tag_is_cleared(self, steps) -> None:
        # Clearing cannot be taken back, so nothing after it may refuse.
        steps.at_cap = True
        db = _Db()
        with pytest.raises(ProjectFileLimitError):
            await project_files.add(db, _project(), _upload())
        assert "cleared" not in steps.order
        assert db.added == []

    async def test_storage_refusing_to_keep_it_fails_the_add(self, steps) -> None:
        steps.refuse_clear = True
        db = _Db()
        with pytest.raises(FileRetentionFailedError):
            await project_files.add(db, _project(), _upload())
        assert db.added == []

    async def test_a_session_file_keeps_its_origin(self, steps, monkeypatch) -> None:
        async def bound(_db, ref, *, user_id):
            return SimpleNamespace(
                session_id=ref.session_id,
                file_key="outputs/s/t/0_chart.png",
                filename=None,
                mime_type="image/png",
            )

        monkeypatch.setattr(project_files, "_session_file", bound)
        body = ProjectFileCreate(
            session_file={"session_id": SESSION_ID, "file_key": "outputs/s/t/0_chart.png"}
        )
        file = await project_files.add(_Db(), _project(), body)

        assert file.origin is ProjectFileOrigin.SESSION_FILE
        assert file.origin_session_id == SESSION_ID
        # An older row without a filename is named from its key.
        assert file.filename == "chart.png"
        assert file.mime_type == "image/png"


class TestSchema:
    def test_exactly_one_source(self) -> None:
        with pytest.raises(ValueError):
            ProjectFileCreate()
        with pytest.raises(ValueError):
            ProjectFileCreate(
                upload={"file_key": "k", "filename": "a.md"},
                session_file={"session_id": SESSION_ID, "file_key": "k"},
            )
