"""Pydantic schemas for the per-message feedback feature.

Owner-side surface only — feedback is private between the session
owner and the AI. Public share view is read-only and never sees the
feedback column.

The wire shape uses the same :class:`FeedbackRating` enum the model
column uses (``"UP" | "DOWN"`` strings), so there's no encoding
boundary to translate at the endpoint.
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.message_feedback import FeedbackRating


class FeedbackUpsert(BaseModel):
    """Body for ``PUT /sessions/{id}/messages/{id}/feedback``.

    ``comment`` cap of 1000 chars matches the product brief — long
    enough for a paragraph of "what went wrong", short enough that we
    don't need pagination or truncation in any UI surface that shows
    feedback back. ``min_length=1`` plus the validator below reject
    empty *and* whitespace-only strings — clients should send ``null``
    when there's no comment, and ``DELETE`` when revoking the entire
    feedback. The validator also strips, so trailing whitespace from
    the textarea doesn't leak into the DB."""

    rating: FeedbackRating
    comment: str | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator("comment")
    @classmethod
    def _strip_or_reject_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError(
                "Comment cannot be blank — send null instead.",
            )
        return stripped


class FeedbackResponse(BaseModel):
    """Owner-side view of a feedback row, also embedded in
    :class:`MessageResponse` so the session-detail GET round-trips
    everything the UI needs to render current state."""

    rating: FeedbackRating
    comment: str | None
    updated_at: datetime

    model_config = {"from_attributes": True}
