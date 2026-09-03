r"""Substring matching for the paginated list endpoints' ``search`` filter.

``GET /sessions``, ``/sessions/files`` and ``/sessions/sites`` all offer
the same ``search`` parameter and must answer identically for the same
input, so the matching is built here rather than per endpoint.
"""

from sqlalchemy import ColumnElement
from sqlalchemy.orm import InstrumentedAttribute

# LIKE's own escape character, passed to ``ilike`` so the escaping
# ``_like_pattern`` applies is actually honoured. Postgres defaults to
# backslash, but only when the clause says so — omit it and every
# escape in the pattern is read as a literal backslash instead.
_ESCAPE = "\\"


def _like_pattern(search: str) -> str:
    r"""Build a ``%…%`` pattern for a case-insensitive substring match.

    Escapes the LIKE wildcards (``%`` / ``_``) and the escape character
    itself, so a literal underscore in a user's query — common in
    filenames and session titles — matches literally instead of standing
    in for any character.
    """
    escaped = search.replace(_ESCAPE, _ESCAPE * 2).replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def ilike_contains(column: InstrumentedAttribute[str | None], search: str) -> ColumnElement[bool]:
    """Case-insensitive "column contains search", wildcards escaped.

    The whole clause rather than just the pattern, because the pattern
    is only half of it: a caller who builds one and forgets ``escape=``
    gets a filter that silently reads the user's ``_`` as a wildcard.
    Nothing at the call site makes that visible, so the two halves are
    not offered separately.

    Typed against ``InstrumentedAttribute`` — the mapped-column form
    every caller passes — rather than the ``ColumnElement`` it is at
    runtime, because SQLAlchemy's stubs model the two separately and
    the wider annotation would need a ``type: ignore`` at each call
    site to no one's benefit.
    """
    return column.ilike(_like_pattern(search), escape=_ESCAPE)
