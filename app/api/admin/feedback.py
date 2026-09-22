"""The console's feedback review queue.

- ``GET   /feedback``       — the queue: filter by status / user, newest first.
- ``PATCH /feedback/{id}``  — triage: OPEN → RESOLVED / IGNORED (or back).

The public surface only ever *creates* feedback (``POST /feedback``,
204); the workflow state on the row exists for this module. There is no
delete — ``IGNORED`` is the dismissal, and the record stays.
"""

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.admin.dependencies import StaffContext
from app.common.pagination import paginate
from app.errors import FeedbackNotFoundError
from app.models.user import User
from app.models.user_feedback import FeedbackStatus, UserFeedback
from app.schemas.admin.feedback import FeedbackResponse, FeedbackUpdate
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.services import user as user_service

router = APIRouter(tags=["feedback"])


def _to_feedback_response(
    feedback: UserFeedback, username: str | None, name: str | None
) -> FeedbackResponse:
    return FeedbackResponse(
        id=feedback.id,
        user_id=feedback.user_id,
        owner_username=username,
        owner_name=name,
        message=feedback.message,
        image_keys=feedback.image_keys,
        status=feedback.status,
        created_at=feedback.created_at,
        updated_at=feedback.updated_at,
    )


@router.get("/feedback", response_model=PaginatedResponse[FeedbackResponse])
async def list_feedback(
    ctx: StaffContext,
    status_: FeedbackStatus | None = Query(None, alias="status"),
    user_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[FeedbackResponse]:
    """Return one page of the review queue, newest first.

    The default view (no filters) is "everything, latest on top"; the
    working view is ``?status=OPEN``, which rides the
    ``ix_user_feedback_status_created`` index built for exactly this
    query. The author is LEFT-joined — a row whose author was deleted
    still belongs in the queue.
    """
    filters = []
    if status_ is not None:
        filters.append(UserFeedback.status == status_)
    if user_id is not None:
        filters.append(UserFeedback.user_id == user_id)

    total = await ctx.db.scalar(select(func.count()).select_from(UserFeedback).where(*filters))
    rows = (
        await ctx.db.execute(
            paginate(
                select(UserFeedback, User.username, User.name)
                .outerjoin(User, User.id == UserFeedback.user_id)
                .where(*filters)
                .order_by(UserFeedback.created_at.desc(), UserFeedback.id.desc()),
                page,
                page_size,
            )
        )
    ).all()

    return PaginatedResponse(
        items=[_to_feedback_response(entry, username, name) for entry, username, name in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.patch("/feedback/{feedback_id}", response_model=FeedbackResponse)
async def update_feedback(
    ctx: StaffContext, feedback_id: int, body: FeedbackUpdate
) -> FeedbackResponse:
    """Move a submission to a new triage status.

    A no-op (status already there) returns the row without writing,
    same as the user PATCH.
    """
    feedback = await ctx.db.get(UserFeedback, feedback_id)
    if feedback is None:
        raise FeedbackNotFoundError(f"No feedback with id {feedback_id}.")

    # Fetched explicitly rather than via the lazy relationship — an
    # async lazy load outside a query context would raise. Both name
    # columns in one row, so the single-row path and the list agree on
    # what an identity is.
    username, name = await user_service.owner_identity(ctx.db, feedback.user_id)

    if feedback.status == body.status:
        return _to_feedback_response(feedback, username, name)

    feedback.status = body.status
    await ctx.db.commit()

    # ``updated_at`` is a server-side ``onupdate`` column, expired by the
    # UPDATE flush — reading it back without this refresh would attempt an
    # async lazy load outside a query context.
    await ctx.db.refresh(feedback)
    return _to_feedback_response(feedback, username, name)
