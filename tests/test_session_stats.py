"""Session aggregates — the decisions in the query.

The conversation count makes choices a later "fix" could undo without
noticing: a conversation is a session with at least one turn, a
soft-deleted one still counts, and the window is cut on ``created_at``.
Pinned against the **compiled SQL** rather than the source text — a
docstring that mentions ``updated_at`` must not turn a test red, and a
filter spelled through a helper must not keep it green.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

from datetime import UTC, datetime

from sqlalchemy.dialects import postgresql

from app.services.session import stats

_SINCE = datetime(2026, 1, 1, tzinfo=UTC)


def _sql() -> str:
    statement = stats.opened_sessions_query(since=_SINCE)
    # Whitespace-normalised: the compiler breaks lines where it likes.
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).split())


def test_a_conversation_is_a_session_somebody_spoke_in() -> None:
    # The client creates the row before the first message goes out, so a
    # failed first send leaves a turnless shell. The sibling rankings
    # count things that happened; so does this one.
    assert (
        "EXISTS (SELECT * FROM session_turn WHERE session_turn.session_id = session.id)" in _sql()
    )


def test_soft_deleted_sessions_count() -> None:
    # Deleted is not the same as never happened, and the platform-wide
    # list shows them too; filtering them here would make the two
    # disagree about how much was talked.
    assert "deleted_at" not in _sql()


def test_the_window_is_cut_on_created_at() -> None:
    # "How many conversations were started" — not when they were last
    # touched. ``updated_at`` moves on every turn and would drag old
    # sessions into whichever window last saw them.
    sql = _sql()
    assert "session.created_at >=" in sql
    assert "updated_at" not in sql


def test_the_count_is_platform_wide() -> None:
    # No join onto ``user``: a conversation whose owner was deleted still
    # happened. ``users`` counts distinct owners and skips the NULL.
    sql = _sql()
    assert "JOIN" not in sql
    assert "count(distinct(session.user_id)) AS users" in sql
