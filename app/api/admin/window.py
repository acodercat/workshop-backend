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
    instead of an error. Every response echoes the window actually used,
    so the caller is never misled about what it got.
    """
    return days if days in ALLOWED_DAYS else DEFAULT_DAYS


WindowDays = Annotated[int, Depends(_resolve_window)]
"""A validated trailing window, already clamped to :data:`ALLOWED_DAYS`."""
