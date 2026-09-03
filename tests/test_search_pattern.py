"""Substring matching behind the list endpoints' ``search`` parameter.

``GET /sessions``, ``/sessions/files`` and ``/sessions/sites`` all build
their filter with :func:`ilike_contains`, so this is the one place the
behaviour is pinned. The escaping used to be copy-pasted per endpoint;
these cases are what a fourth copy would have been free to get subtly
wrong.

Assertions read the pattern and the escape character back off the
generated expression rather than compiling to SQL: the psycopg dialect
doubles ``%`` for its own paramstyle, which would put driver quoting in
the middle of a test about wildcard escaping.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

from app.common.search import ilike_contains
from app.models.session import Session


def pattern_for(search: str) -> str:
    return str(ilike_contains(Session.title, search).right.value)


def test_a_plain_query_is_wrapped_for_substring_matching() -> None:
    assert pattern_for("report") == "%report%"


def test_the_clause_carries_the_escape_character() -> None:
    # The half a call site is free to forget. Without it Postgres reads
    # every backslash in the pattern as a literal, and the escaping
    # below silently stops meaning anything.
    assert ilike_contains(Session.title, "q3_report").modifiers["escape"] == "\\"


def test_the_match_is_case_insensitive() -> None:
    # Users type "Report" for a session titled "report". ILIKE, not LIKE.
    assert ilike_contains(Session.title, "report").operator.__name__ == "ilike_op"


def test_underscore_is_escaped_so_it_matches_itself() -> None:
    # Without the escape, "q3_report" would also match "q3Xreport" —
    # underscore is LIKE's single-character wildcard. Session titles and
    # filenames are full of them.
    assert pattern_for("q3_report") == "%q3\\_report%"


def test_percent_is_escaped_so_it_cannot_widen_the_match() -> None:
    # An unescaped "%" makes the query match everything around it, which
    # reads to the user as "search is broken", not as a wildcard feature.
    assert pattern_for("50%") == "%50\\%%"


def test_backslash_is_escaped_first_so_it_cannot_eat_the_next_escape() -> None:
    # Order matters: escaping "%" before "\" would turn the user's literal
    # backslash into the escape for the wildcard that follows it, and the
    # pattern would stop matching what it says.
    assert pattern_for("a\\_b") == "%a\\\\\\_b%"


def test_an_all_wildcard_query_matches_literally_not_everything() -> None:
    assert pattern_for("%_%") == "%\\%\\_\\%%"


def test_spaces_and_unicode_pass_through_untouched() -> None:
    # Only the three LIKE metacharacters are special; a Chinese title or a
    # spaced phrase must reach the database exactly as typed.
    assert pattern_for("季度 报告") == "%季度 报告%"
