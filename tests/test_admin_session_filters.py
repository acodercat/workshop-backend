"""The session list's window filter, and what it cuts on.

Read off the **compiled SQL** the endpoint actually builds, not off a
fake result set: a fake returns whatever it was handed and would agree
with any predicate. The db here records the statements instead, so the
assertions are about the ``WHERE`` clause that would reach Postgres.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import ast
import inspect
import textwrap

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.admin import session as admin_session
from app.api.admin.window import ALLOWED_DAYS, _resolve_optional_window
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.staff import Staff, StaffStatus


class _RecordingDb:
    """Answers nothing and remembers everything: the endpoint's own
    statements are the subject under test."""

    def __init__(self) -> None:
        self.statements: list = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return 0

    async def execute(self, statement):
        self.statements.append(statement)

        class _Empty:
            def all(self):
                return []

        return _Empty()


def _where(query: str) -> str:
    """The compiled list query's ``WHERE`` clause, whitespace-normalised."""
    db = _RecordingDb()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_session.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[authenticate_and_get_staff] = lambda: Staff(
        id=7,
        username="alice",
        password_hash="x",
        name="Alice",
        status=StaffStatus.ACTIVE,
        token_version=0,
    )
    assert TestClient(app).get(f"/sessions{query}").status_code == 200
    # The count comes first, the page second; either carries the filters.
    sql = " ".join(str(db.statements[-1].compile(dialect=postgresql.dialect())).split())
    return sql[sql.index("WHERE") :] if "WHERE" in sql else ""


def _source() -> str:
    return inspect.getsource(admin_session.list_sessions)


def test_the_window_cuts_on_the_column_the_list_shows() -> None:
    # The list is sorted by ``updated_at`` and shows it as its last
    # column. Cutting on ``created_at`` would hide rows whose visible
    # date is inside the window, and would leave the index that already
    # serves the sort unable to serve the filter.
    source = _source()
    assert "Session.updated_at >= window_start(days)" in source
    assert "Session.created_at" not in source


def test_the_count_and_the_page_share_the_filters() -> None:
    """A total that answered a wider question than the rows beneath it
    would put a page count on a list that cannot fill it."""
    tree = ast.parse(textwrap.dedent(_source()))
    wheres = [
        ast.unparse(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "where"
        and node.args
    ]
    # One on the count, one on the page, both the same list.
    assert wheres == ["*filters", "*filters"], wheres


def test_no_window_means_every_session() -> None:
    """A list's default is everything; the window is a filter an
    operator reaches for, not a span the page always has."""
    assert _resolve_optional_window(days=None) is None


def test_an_unoffered_window_falls_back_to_no_window_not_to_the_default() -> None:
    """The two fallbacks look alike and are not. For a page that must
    have a span, 30 days is a different view; here it would be *narrower*
    than asking for nothing, so a hand-widened ``?days=365`` would answer
    with fewer rows than the plain list and nothing in the response would
    say a window was applied."""
    for unoffered in (2, 45, 180, 365):
        assert _resolve_optional_window(days=unoffered) is None
    for offered in ALLOWED_DAYS:
        assert _resolve_optional_window(days=offered) == offered


def test_no_filters_means_no_where_clause() -> None:
    assert _where("") == ""


def test_the_window_becomes_a_bound_on_updated_at() -> None:
    assert "session.updated_at >=" in _where("?days=7")


def test_an_unoffered_window_reaches_the_query_as_no_filter() -> None:
    """The resolver's fallback, seen from the other end: ``?days=365``
    must widen to everything rather than quietly narrowing to 30 days."""
    assert _where("?days=365") == ""
