"""The query shapes behind the /api/projects surface that carry an invariant
a reader cannot see from the call site. CRUD itself is exercised end-to-end
against a live stack."""

import uuid

from sqlalchemy.dialects import postgresql

from app.api.public import project as endpoint


def _sql(statement) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


class TestMovedOut:
    """Deleting a project moves its sessions out instead of deleting them."""

    def test_moves_out_every_session_pointing_at_the_project(self) -> None:
        project_id = uuid.uuid4()
        sql = _sql(endpoint._moved_out(project_id))
        assert "SET project_id=NULL" in sql
        # Soft-deleted sessions too: nothing may be left pointing at a
        # deleted project.
        assert f"WHERE session.project_id = '{project_id}'" in sql
        assert "deleted_at" not in sql

    def test_keeps_each_sessions_updated_at(self) -> None:
        # ``onupdate`` would stamp now() and lift every moved session to the
        # top of the recent list.
        sql = _sql(endpoint._moved_out(uuid.uuid4()))
        assert "updated_at=session.updated_at" in sql
