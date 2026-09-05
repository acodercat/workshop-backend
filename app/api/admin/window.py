"""The trailing window the console's report pages offer.

A closed set rather than a free integer: every option should be one an
operator actually reasons in, and it keeps each query's cost
predictable. Shared by the credits dashboard and the payments page so
the two describe the same spans of time — duplicating the policy is how
one page silently starts offering a window the other doesn't.

Expressed as a dependency rather than a helper each endpoint remembers
to call: ``days: WindowDays`` both declares the query parameter and
hands back the *resolved* value, so an endpoint cannot accidentally use
the raw one.
"""

from typing import Annotated

from fastapi import Depends, Query

ALLOWED_DAYS = (7, 30, 90)
DEFAULT_DAYS = 30


def _resolve_window(
    days: int = Query(DEFAULT_DAYS, description=f"Trailing window in days: {ALLOWED_DAYS}"),
) -> int:
    """Clamp a requested window to the offered set.

    An unsupported value falls back rather than 422s — this is an
    internal tool, and a stale bookmark should show the default view
    instead of an error.

    The responses shaped as an object echo the window actually used, so a
    stale ``?days=45`` is visible rather than silent. The ones that are
    not do not: the four leaderboards answer with a bare list and the
    ratings queue with a ``PaginatedResponse``, and reshaping any of them
    purely to carry ``days`` back would cost an envelope for a field only
    this fallback makes interesting. Worth knowing when reading one of
    them: a bookmarked window that is not in ``ALLOWED_DAYS`` shows 30
    days without saying so.
    """
    return days if days in ALLOWED_DAYS else DEFAULT_DAYS


WindowDays = Annotated[int, Depends(_resolve_window)]
"""A validated trailing window, already clamped to :data:`ALLOWED_DAYS`."""
