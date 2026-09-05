"""Per-message ratings — the thumbs users gave individual replies.

- ``GET /message-feedback?rating=&days=`` — the review queue.
- ``GET /message-feedback/stats?days=``   — up / down / down-rate.

Separate from :mod:`app.api.admin.feedback`, which is freeform product
feedback the user chose to write. Both are "what users think"; only this
one is attached to a specific answer, which is what makes it the
platform's quality signal.

Read-only: a rating is the user's opinion, and there is nothing for an
operator to change about it. The action it prompts happens elsewhere —
fix the skill, adjust the prompt.

**The excerpt is the only customer conversation content the console can
show, and it is deliberately the smallest possible slice**: the one reply
being judged, quoted as evidence. There is no way to open the session it
came from — that endpoint was removed rather than audited — so this is
where the privacy line sits, and it is worth keeping it here. Not
audited: the queue is a list the operator scans in bulk, and auditing
every scroll would bury the signal that matters.
"""

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.admin.dependencies import StaffContext
from app.api.admin.window import WindowDays
from app.common.business_time import window_start
from app.common.pagination import paginate
from app.models.message import Message
from app.models.message_feedback import FeedbackRating, MessageFeedback
from app.models.user import User
from app.schemas.admin.message_feedback import (
    MessageRatingResponse,
    MessageRatingStatsResponse,
)
from app.schemas.collection_response import PaginatedResponse, PaginationMeta

router = APIRouter(tags=["message-feedback"])

# How much of the rated reply to carry into the queue. Enough to
# recognise the answer, short enough that a page of them stays scannable.
_EXCERPT_CHARS = 240


def _excerpt(blocks: list[dict]) -> str:
    """The head of a message's first text block.

    Blocks are a discriminated list — text, tool_use, tool_result,
    error. Only the text carries something a human recognises the answer
    by; a reply that is all tool calls yields a marker rather than an
    empty string, so the row still reads as a real message.
    """
    for block in blocks or []:
        if block.get("type") == "text" and block.get("content"):
            content = str(block["content"]).strip()
            return content[:_EXCERPT_CHARS] + "…" if len(content) > _EXCERPT_CHARS else content
    return "(no text in this reply)"


@router.get(
    "/message-feedback",
    response_model=PaginatedResponse[MessageRatingResponse],
)
async def list_message_feedback(
    ctx: StaffContext,
    days: WindowDays,
    rating: FeedbackRating | None = Query(
        None, description="Omit for both; DOWN is the working queue"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[MessageRatingResponse]:
    """Return one page of ratings, newest first.

    Joined to the rated message so each row carries its excerpt and the
    session it belongs to — a rating on its own says a reply was bad
    without saying which reply.
    """
    filters = [MessageFeedback.created_at >= window_start(days)]
    if rating is not None:
        filters.append(MessageFeedback.rating == rating)

    total = await ctx.db.scalar(select(func.count()).select_from(MessageFeedback).where(*filters))
    rows = (
        await ctx.db.execute(
            paginate(
                select(MessageFeedback, Message, User.username)
                .join(Message, Message.id == MessageFeedback.message_id)
                .join(User, User.id == MessageFeedback.user_id)
                .where(*filters)
                .order_by(MessageFeedback.created_at.desc()),
                page,
                page_size,
            )
        )
    ).all()

    return PaginatedResponse(
        items=[
            MessageRatingResponse(
                id=feedback.id,
                rating=feedback.rating,
                comment=feedback.comment,
                user_id=feedback.user_id,
                username=username,
                session_id=message.session_id,
                message_id=message.id,
                message_excerpt=_excerpt(message.blocks),
                created_at=feedback.created_at,
            )
            for feedback, message, username in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.get("/message-feedback/stats", response_model=MessageRatingStatsResponse)
async def read_message_feedback_stats(
    ctx: StaffContext, days: WindowDays
) -> MessageRatingStatsResponse:
    """Return rating counts for the window.

    ``down_rate`` is a share of *rated* replies — see the schema for why
    it must not be read as a quality score.
    """
    totals = (
        await ctx.db.execute(
            select(
                func.count().filter(MessageFeedback.rating == FeedbackRating.UP).label("up"),
                func.count().filter(MessageFeedback.rating == FeedbackRating.DOWN).label("down"),
            )
            .select_from(MessageFeedback)
            .where(MessageFeedback.created_at >= window_start(days))
        )
    ).one()

    rated = totals.up + totals.down
    return MessageRatingStatsResponse(
        days=days,
        up=totals.up,
        down=totals.down,
        down_rate=round(totals.down / rated, 4) if rated else 0.0,
    )
