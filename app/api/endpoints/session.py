"""HTTP adapters for session resources.

Auth policy — every session operation requires the caller to own the
session. Each handler injects an auth dependency that returns the
``User`` object: :func:`authenticate_and_get_user` for normal
request/response endpoints (request-scoped DB session), and
:func:`authenticate_for_streaming` for the SSE endpoint (short-lived
DB session that closes before the stream begins, avoiding
``idle_in_transaction_session_timeout`` on the held connection). SSE
clients still send ``Authorization`` because the frontend uses
``@microsoft/fetch-event-source``.

DB queries live inline in the handlers; the two genuinely shared query
helpers (``_get_or_404``, ``_load_messages``) sit at module top.
Non-trivial logic — message enqueue with its ordering invariants and
SSE streaming — lives in :mod:`app.services.session`.
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from openai import OpenAIError
import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import get_db, get_redis, get_redis_streaming
from app.auth import authenticate_and_get_user, authenticate_for_streaming
from app.database import AsyncSessionLocal
from config import settings
from app.common.pagination import paginate
from app.models.message import Message
from app.models.session import Session, SessionStatus
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.session import (
    MessageCreate,
    MessageQueuedResponse,
    MessageResponse,
    SessionCreate,
    SessionDetailResponse,
    SessionResponse,
    SessionUpdate,
    TitleGenerateRequest,
    TitleGenerateResponse,
)
from app.services.redis import SESSION_STREAM_PREFIX, cancel_channel, stream_key
from app.services.session import generate_title, queue_user_message, stream_events
from app.services.session.exceptions import GenerationInProgressError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sessions"])


async def _get_or_404(db: AsyncSession, session_id: str, user_id: int) -> Session:
    """Return the session row iff it exists AND the caller owns it.

    404 (not 403) on an ownership miss so the existence of other users'
    session ids isn't leaked.
    """
    session = await db.get(Session, session_id)
    if not session or session.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def _load_messages(db: AsyncSession, session_id: str) -> list[MessageResponse]:
    """Load a session's messages, projected into the response shape."""
    stmt = (
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        MessageResponse(
            id=row.id,
            role=row.role.value,
            blocks=row.blocks,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    model_id = body.model_id or settings.default_session_model_id
    if model_id not in settings.supported_session_model_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported model_id: {model_id!r}",
        )
    session = Session(
        title=body.title,
        user_id=current_user.id,
        model_id=model_id,
        # Explicit IDLE — the model carries no default, so omitting
        # this would IntegrityError on INSERT. IDLE is the honest
        # initial state (vs the previous misleading default of COMPLETE).
        status=SessionStatus.IDLE,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@router.get("/sessions", response_model=PaginatedResponse[SessionResponse])
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    base = (
        select(Session)
        .where(Session.user_id == current_user.id)
        .order_by(Session.updated_at.desc())
    )
    total = (
        await db.execute(
            select(func.count(Session.id)).where(Session.user_id == current_user.id)
        )
    ).scalar() or 0
    rows = (await db.execute(paginate(base, page, page_size))).scalars().all()
    return PaginatedResponse(
        items=list(rows),
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Return the full session payload: metadata + status + structured messages.

    Single endpoint the frontend uses to render a session on open. If the
    response's ``status == "RUNNING"``, the client should additionally
    open ``/sessions/{id}/stream`` to watch the in-flight turn; otherwise
    the detail response is complete on its own.
    """
    session = await _get_or_404(db, session_id, current_user.id)
    messages = await _load_messages(db, session_id)
    return SessionDetailResponse(
        id=session.id,
        title=session.title,
        status=session.status,
        model_id=session.model_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=messages,
    )


@router.patch("/sessions/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str,
    body: SessionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    session = await _get_or_404(db, session_id, current_user.id)
    if body.title is not None:
        session.title = body.title
    await db.commit()
    await db.refresh(session)
    return session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Delete a session. Refuses while a generation is in progress.

    Deleting mid-turn would orphan the running worker (assistant INSERT
    would hit a dangling FK) and leak the Redis output stream. Caller
    must wait for ``status`` to leave ``running`` before retrying.

    DB is deleted first (it's the source of truth for the user-visible
    session list); the Redis output stream is then cleaned up. A Redis
    failure during cleanup is logged but does not fail the request —
    the worker's ``expire_stream`` TTL will reap the orphan eventually.
    """
    session = await _get_or_404(db, session_id, current_user.id)
    if session.status == SessionStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a session while a generation is in progress",
        )
    await db.delete(session)
    await db.commit()

    try:
        await redis.delete(stream_key(SESSION_STREAM_PREFIX, session_id))
    except Exception:
        logger.warning(
            "Failed to delete Redis stream for session %s — TTL will reap it",
            session_id,
            exc_info=True,
        )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=MessageQueuedResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(settings.rate_limit_message_create)
async def create_message(
    request: Request,
    session_id: str,
    body: MessageCreate,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Create a user message and enqueue LLM generation to the worker.

    Service-layer domain errors translate to HTTP here so the service
    stays unaware of FastAPI: ``GenerationInProgressError`` → 409. The
    DB-lease design has no Redis-enqueue failure path: ``publish_wake``
    is best-effort and never raises into this handler.
    """
    session = await _get_or_404(db, session_id, current_user.id)
    try:
        await queue_user_message(
            db, redis, session, content=body.content, files=body.files,
        )
    except GenerationInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        )
    return MessageQueuedResponse()


@router.post(
    "/sessions/{session_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Request cancellation of an in-progress session.

    Returns 202 immediately — the cancel is asynchronous. The worker
    holding the session picks up the pub/sub signal, fires
    :meth:`axon.Agent.cancel`, and the final ``agent.stopped`` event
    (with ``stop_reason="cancelled"``) arrives to the client via the
    existing SSE stream. Completed / failed sessions 409.
    """
    session = await _get_or_404(db, session_id, current_user.id)
    if session.status != SessionStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session is not running",
        )
    await redis.publish(cancel_channel(session_id), "")


@router.get("/sessions/{session_id}/stream")
async def stream_session(
    session_id: str,
    redis: aioredis.Redis = Depends(get_redis_streaming),
    current_user: User = Depends(authenticate_for_streaming),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """Stream live SSE events for an in-flight generation.

    Cold history (completed turns) is served exclusively by
    ``GET /sessions/{id}``; this endpoint is only meaningful while a
    generation is actively running. Clients should call it iff the
    session payload reports ``status == "RUNNING"``.

    Both auth and ownership check use short-lived DB sessions that close
    before the SSE stream begins — see :func:`authenticate_for_streaming`
    for the rationale. The stream itself reads from Redis only.

    ``Last-Event-ID`` is sent automatically by the browser's
    ``EventSource`` on reconnect; passing it through resumes the
    stream after that entry instead of re-replaying everything (the
    reducer is not idempotent on chunked events — see
    :func:`stream_events` for the full rationale).
    """
    async with AsyncSessionLocal() as db:
        await _get_or_404(db, session_id, current_user.id)
    return EventSourceResponse(
        stream_events(redis, session_id, last_event_id=last_event_id)
    )


@router.get(
    "/sessions/{session_id}/messages",
    response_model=list[MessageResponse],
    deprecated=True,
)
async def list_messages(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Deprecated — use ``GET /sessions/{id}`` for the structured payload."""
    await _get_or_404(db, session_id, current_user.id)
    return await _load_messages(db, session_id)


@router.post("/titles", response_model=TitleGenerateResponse)
async def post_title(
    body: TitleGenerateRequest,
) -> TitleGenerateResponse:
    """Stateless LLM-backed title generator for a conversation.

    Takes the user's first-message content and returns a short title.
    No session_id required and no DB writes — the frontend persists
    the result via ``PATCH /sessions/{id}`` once the session row
    exists. Splitting the two lets the sidebar show a real title as
    soon as the user hits send, without waiting for the agent turn.

    Auth inherits the router-level ``authenticate_and_get_user``
    dependency — any logged-in user can call it; no further scoping
    is meaningful since there's no resource being read or written.
    """
    try:
        title = await generate_title(body.content)
    except OpenAIError:
        logger.exception("Title generation failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Title generation failed",
        )
    return TitleGenerateResponse(title=title)
