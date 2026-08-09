"""Viewing a session's content from the console.

``GET /sessions/{id}`` — metadata + the rendered message blocks,
read-only. The per-user session *list* lives with the user resource
(``GET /users/{id}/sessions`` in :mod:`app.api.admin.user`); this module
is only the content view.

**This read is audited** (``session.view``) — the only audited read in
the console. It is customer conversation content, the most
privacy-sensitive thing the console can show, so "who looked at whose
conversation" must stay answerable. The audit block wraps no mutation;
committing the row is the whole point.

**No cancel, no delete.** Cancel would need a Redis client in this
process (deliberately absent) and stuck turns are already recovered by
the worker's stale-lease cron and the budget enforcer; delete is the
user's own action.
"""

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.api.admin.dependencies import StaffContext
from app.errors import SessionNotFoundError
from app.models.message import Message
from app.models.session import Session
from app.schemas.admin.session import MessageResponse, SessionDetailResponse
from app.services.admin import AuditAction, audit

router = APIRouter(tags=["sessions"])


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def read_session(
    ctx: StaffContext, session_id: uuid.UUID
) -> SessionDetailResponse:
    """Return one session with its full message history, soft-deleted or
    not — a PK lookup with no ``deleted_at`` filter, because "what was in
    the session they deleted" is a question support actually gets.

    The audit payload carries the owner's ``user_id`` so the trail
    answers "who looked at whose conversation", not just at which
    session id.
    """
    session = await ctx.db.get(Session, session_id)
    if session is None:
        raise SessionNotFoundError(f"No session with id {session_id}.")

    messages = (
        await ctx.db.scalars(
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(Message.id)
        )
    ).all()

    # Audited after the content is in hand: a failed load must not leave
    # a row claiming a view that never made it to the operator's screen.
    async with audit(
        ctx,
        action=AuditAction.SESSION_VIEW,
        target_type="session",
        target_id=str(session.id),
    ) as draft:
        draft.payload = {"user_id": session.user_id}

    return SessionDetailResponse(
        id=session.id,
        user_id=session.user_id,
        title=session.title,
        status=session.status,
        tier=session.tier,
        agent_type=session.agent_type,
        created_at=session.created_at,
        updated_at=session.updated_at,
        deleted_at=session.deleted_at,
        messages=[MessageResponse.model_validate(m) for m in messages],
    )
