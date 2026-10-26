"""User-feedback endpoints.

Single endpoint: ``POST /feedback`` lets an authenticated user submit
free-text feedback (with optional pre-uploaded image attachments). The
review side lives on the console surface (:mod:`app.api.admin.feedback`)
— this module only creates rows; the workflow ``status`` on them is the
console's to manage.
"""

import logging

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.models.user import User
from app.models.user_feedback import UserFeedback
from app.rate_limit import limiter
from app.schemas.user_feedback import FeedbackSubmitRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/feedback", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def submit_feedback(
    request: Request,
    body: FeedbackSubmitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> None:
    """Persist user feedback. Returns 204 — clients only need success
    confirmation; the persisted row's id is admin-side metadata and
    never echoed back.

    Rate-limited to 5/hour per user to throttle spam without choking
    legitimate sequential reports of related issues.
    """
    feedback = UserFeedback(
        user_id=current_user.id,
        message=body.message,
        image_keys=body.image_keys,
    )
    db.add(feedback)
    await db.commit()
    logger.info(
        "feedback submitted: id=%s user_id=%s images=%d",
        feedback.id,
        current_user.id,
        len(body.image_keys),
    )
