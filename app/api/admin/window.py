"""The trailing window the console's report pages offer.

A closed set rather than a free integer: every option should be one an
operator actually reasons in, and it keeps each query's cost
predictable. Shared by the credits dashboard, the payments page, the
message-ratings page and the session list so they describe the same
spans of time —
duplicating the policy is how one page silently starts offering a window
the other doesn't. The list takes :data:`OptionalWindowDays`, which adds
"all time" without forking the set.

``1`` is *today*: :func:`app.common.business_time.window_start` aligns
a window to the start of its first business day, so one day is the
current day so far, not the last 24 hours. The dashboard's snapshot
tiles ask it; the report pages offer the other three.

Expressed as a dependency rather than a helper each endpoint remembers
to call: ``days: WindowDays`` both declares the query parameter and
hands back the *resolved* value, so an endpoint cannot accidentally use
the raw one.
"""

from typing import Annotated

from fastapi import Depends, Query

ALLOWED_DAYS = (1, 7, 30, 90)
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


def _resolve_optional_window(
    days: int | None = Query(
        None, description=f"Trailing window in days: {ALLOWED_DAYS}; omit for all time"
    ),
) -> int | None:
    """The same closed set, plus "no window at all".

    The report pages always cover *some* window — a dashboard with no
    span is not a dashboard. A list is the other shape: its default is
    everything, and the window is a filter an operator reaches for. So
    ``None`` is a real answer here and is not clamped away.

    An unoffered *integer* falls back to no window, **not** to
    :data:`DEFAULT_DAYS`. The two fallbacks look alike and are not: for
    a page that must have a span, 30 days is simply a different view;
    here it would be narrower than asking for nothing, so a hand-widened
    ``?days=365`` would answer with *fewer* rows than the plain list and
    nothing in the response says a window was applied — which reads as
    "there is no older activity". Falling back to the endpoint's own
    default is the only fallback that cannot hide data.

    A non-integer (``?days=`` or ``?days=abc``) is still a 422 — the
    parameter is typed, and coercing it here would only move the same
    422 into a wider guess.
    """
    if days is None or days not in ALLOWED_DAYS:
        return None
    return days


OptionalWindowDays = Annotated[int | None, Depends(_resolve_optional_window)]
"""A trailing window, or ``None`` for no window at all."""
