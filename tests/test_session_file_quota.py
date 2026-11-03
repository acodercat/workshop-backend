"""Which objects the per-user upload quota counts (``_enforce_session_file_quota``).

Asserted on the compiled statement: what the quota charges for is entirely
in its predicates, and a stub of the query would pass whatever they said.
"""

from sqlalchemy.dialects import postgresql

from app.api.public.session import _enforce_session_file_quota


class _Result:
    def one(self):
        return (0, 0)


class _CapturingDb:
    sql = ""

    async def execute(self, statement):
        self.sql = str(
            statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        return _Result()


async def _compiled() -> str:
    db = _CapturingDb()
    await _enforce_session_file_quota(db, 7, 0)
    return db.sql


async def test_files_the_user_added_to_a_project_count() -> None:
    # An upload on a project's page may never be bound to a session, and a
    # session file added to a project outlives the session: the session
    # tables alone would let either bypass the quota once that is deleted.
    sql = await _compiled()
    assert "FROM project_file" in sql
    assert "project_file.user_id = 7" in sql
    assert "project_file.origin != 'AGENT'" in sql


async def test_deleting_a_project_file_or_its_project_frees_the_quota() -> None:
    sql = await _compiled()
    assert "project_file.deleted_at IS NULL" in sql
    assert "project.deleted_at IS NULL" in sql


async def test_an_object_counts_once_wherever_it_is_bound() -> None:
    # A session file added to a project is the same object.
    sql = await _compiled()
    assert "GROUP BY" in sql
    assert sql.count("UNION ALL") == 2
