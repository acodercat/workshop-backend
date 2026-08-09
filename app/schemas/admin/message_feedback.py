"""Response models for per-message ratings.

Distinct from :mod:`app.schemas.admin.feedback`, which is the freeform
product feedback a user chooses to send. This is the thumb they gave one
assistant reply — the platform's only per-answer quality signal, and
only useful next to the answer it judges.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.message_feedback import FeedbackRating


class MessageRatingResponse(BaseModel):
    """One rating, with enough of the rated reply to triage it.

    ``message_excerpt`` is the head of the assistant message's first
    text block. A rating without the text it judges is unactionable, and
    the full transcript is one click away at ``session_id`` — so this
    carries the excerpt, not the message.
    """

    id: int
    rating: FeedbackRating
    comment: str | None
    user_id: int
    username: str
    session_id: UUID | None
    message_id: int
    message_excerpt: str
    created_at: datetime


class MessageRatingStatsResponse(BaseModel):
    """Rating counts for the window.

    ``down_rate`` is the share of *rated* replies marked down — not of
    all replies, the vast majority of which are never rated at all.
    Reading it as a quality score would badly overstate dissatisfaction;
    it tracks movement in the rated sample.
    """

    days: int
    up: int
    down: int
    down_rate: float
