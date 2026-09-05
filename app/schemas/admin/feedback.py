"""Request / response models for the console's feedback review queue.

One response shape for both the list and the PATCH result: a feedback
row is small enough (message ≤ 2000 chars) that the list rows are
already complete, so there is no separate detail view to model.
"""

from datetime import datetime

from pydantic import BaseModel

from app.models.user_feedback import FeedbackStatus


class FeedbackResponse(BaseModel):
    """One feedback submission, as the review queue renders it.

    ``owner_username`` is joined in so the queue answers "who sent this"
    without a second lookup; both it and ``user_id`` are ``None`` for
    rows whose author was deleted (the FK is ``SET NULL`` so the
    feedback outlives the account).

    ``image_keys`` are the raw OSS keys — minting presigned view URLs
    is the console UI's follow-up, not this endpoint's concern.
    """

    id: int
    user_id: int | None
    owner_username: str | None
    message: str
    image_keys: list[str]
    status: FeedbackStatus
    created_at: datetime
    updated_at: datetime


class FeedbackUpdate(BaseModel):
    """Move a submission through the triage workflow.

    ``status`` is the whole request: marking feedback RESOLVED or
    IGNORED is benign triage whose meaning is the status itself.
    """

    status: FeedbackStatus
