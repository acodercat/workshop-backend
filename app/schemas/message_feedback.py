"""Pydantic schemas for the per-message feedback feature.

Owner-side surface only — feedback is private between the session
owner and the AI. Public share view is read-only and never sees the
feedback column.

Wire format uses ``"up"/"down"`` strings rather than the DB's numeric
``1/-1`` because string enums are clearer in JSON and the translation
is one helper away. ``serialize_rating`` / ``parse_rating`` keep the
mapping in one place so the model layer never has to know about the
wire shape and the wire layer never has to know about ``SmallInteger``.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---- value-object helpers ------------------------------------------------

Rating = Literal["up", "down"]

_RATING_TO_INT = {"up": 1, "down": -1}
_INT_TO_RATING: dict[int, Rating] = {1: "up", -1: "down"}


def parse_rating(value: Rating) -> int:
    """``"up" | "down"`` → ``1 | -1`` for DB storage."""
    return _RATING_TO_INT[value]


def serialize_rating(value: int) -> Rating:
    """``1 | -1`` (from DB) → ``"up" | "down"`` for wire."""
    return _INT_TO_RATING[value]


# ---- request / response schemas -----------------------------------------


class FeedbackUpsert(BaseModel):
    """Body for ``PUT /sessions/{id}/messages/{id}/feedback``.

    ``comment`` cap of 1000 chars matches the product brief — long
    enough for a paragraph of "what went wrong", short enough that we
    don't need pagination or truncation in any UI surface that shows
    feedback back. Empty string is treated as ``None`` (use the
    DELETE endpoint to clear, not an empty comment)."""

    rating: Rating
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackResponse(BaseModel):
    """Owner-side view of a feedback row, also embedded in
    :class:`MessageResponse` so the session-detail GET round-trips
    everything the UI needs to render current state."""

    rating: Rating
    comment: str | None
    updated_at: datetime
