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
import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from openai import OpenAIError
import redis.asyncio as aioredis
import sqlalchemy as sa
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import get_db, get_redis, get_redis_streaming
from app.auth import (
    authenticate_and_get_user,
    authenticate_for_streaming,
    get_password_hash,
)
from app.database import AsyncSessionLocal
from config import settings
from app.common.pagination import paginate
from app.models.message import Message, MessageRole
from app.models.message_feedback import MessageFeedback
from app.models.session import Session, SessionStatus
from app.models.session_share import SessionShare
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.file import DownloadUrlResponse
from app.schemas.message_feedback import FeedbackResponse, FeedbackUpsert
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
from app.schemas.session_share import ShareCreate, ShareResponse
from app.services import file_storage
from app.services.redis import SESSION_STREAM_PREFIX, cancel_channel, stream_key
from app.services.session import generate_title, queue_user_message, stream_events
from app.services.session.exceptions import (
    FileNotInSessionError,
    GenerationInProgressError,
)
from app.services.session.files import assert_file_belongs_to_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sessions"])


async def _get_or_404(
    db: AsyncSession, session_id: str | uuid.UUID, user_id: int
) -> Session:
    """Return the session row iff it exists AND the caller owns it.

    404 (not 403) on an ownership miss so the existence of other users'
    session ids isn't leaked.
    """
    session = await db.get(Session, session_id)
    if not session or session.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def _load_messages(
    db: AsyncSession, session_id: str, user_id: int,
) -> list[MessageResponse]:
    """Load a session's messages, projected into the response shape.

    LEFT JOINs ``message_feedback`` filtered by ``user_id`` so each
    message ships with the current user's feedback (or ``None`` if
    they haven't voted). The filter goes *into* the join condition,
    not a WHERE clause — otherwise messages without a feedback row
    would be dropped from the result.
    """
    stmt = (
        select(Message, MessageFeedback)
        .outerjoin(
            MessageFeedback,
            sa.and_(
                MessageFeedback.message_id == Message.id,
                MessageFeedback.user_id == user_id,
            ),
        )
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        MessageResponse(
            id=message.id,
            role=message.role.value,
            blocks=message.blocks,
            created_at=message.created_at,
            feedback=(
                FeedbackResponse.model_validate(feedback)
                if feedback is not None
                else None
            ),
        )
        for message, feedback in rows
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
    messages = await _load_messages(db, session_id, current_user.id)
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
    return await _load_messages(db, session_id, current_user.id)


@router.post("/sessions/titles", response_model=TitleGenerateResponse)
async def generate_session_title(
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


# ---- session share (owner-side) ------------------------------------------
#
# Singleton sub-resource of the session: one share per session.
# Public-side read access for share recipients lives in
# :mod:`app.api.endpoints.share`; the two surfaces deliberately don't
# overlap on auth (user JWTs are rejected on the public path, share-
# view tokens are rejected here).


@router.post(
    "/sessions/{session_id}/share",
    response_model=ShareResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_share(
    session_id: uuid.UUID,
    body: ShareCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Create or rotate the share for ``session_id``.

    Idempotent at the URL level (the resource at
    ``/sessions/{id}/share`` is replaced) but not at the token level —
    each call mints a fresh token, which is the point of "rotate":
    the previous URL stops working. The replace happens in one
    transaction: any prior row is deleted before the new one is
    inserted.
    """
    session = await _get_or_404(db, session_id, current_user.id)

    await db.execute(
        delete(SessionShare).where(SessionShare.session_id == session.id)
    )

    password_hash = (
        await get_password_hash(body.password) if body.password else None
    )
    share = SessionShare(
        session_id=session.id,
        token=secrets.token_urlsafe(32),
        password_hash=password_hash,
    )
    db.add(share)
    await db.commit()
    await db.refresh(share)
    return share


@router.get(
    "/sessions/{session_id}/share",
    response_model=ShareResponse,
)
async def get_session_share(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Return the session's current share, or 404 if there isn't one."""
    await _get_or_404(db, session_id, current_user.id)

    stmt = select(SessionShare).where(SessionShare.session_id == session_id)
    share = (await db.execute(stmt)).scalar_one_or_none()
    if share is None:
        raise HTTPException(status_code=404, detail="Share not found")
    return share


@router.delete(
    "/sessions/{session_id}/share",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_share(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Delete the session's share. No-op if none exists, so retries
    from a flaky client are safe."""
    await _get_or_404(db, session_id, current_user.id)
    await db.execute(
        delete(SessionShare).where(SessionShare.session_id == session_id)
    )
    await db.commit()


# ---- session file download (presigned URL + ownership check) -------------
#
# Authorization is a single ``session_files`` lookup —
# ``(session_id, file_key)`` is the FK invariant set up at write time
# (user uploads in ``services/session/enqueue.py``, tool outputs in
# the worker before its SSE emit). Upload (session-agnostic — the
# frontend may collect a file_key before knowing which session will
# reference it) lives in :mod:`app.api.endpoints.file`.


@router.get(
    "/sessions/{session_id}/files/download-url",
    response_model=DownloadUrlResponse,
)
async def get_download_url(
    session_id: uuid.UUID,
    file_key: Annotated[str, Query(description="OSS file key returned from the upload step")],
    filename: Annotated[str, Query(description="Filename for Content-Disposition")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Mint a presigned GET URL for a file referenced by ``session_id``.

    URL is intentionally **not** ``/files/{file_key}``: the response is
    a download-URL string, not the file's bytes. Putting ``file_key``
    in the path would advertise "GET this URI returns the file" which
    is a lie — what we actually return is a presigned URL the caller
    must follow. ``download-url`` is the resource being minted;
    ``file_key`` and ``filename`` are parameters to that minting.

    Two-step authorization: ``_get_or_404`` verifies the caller owns
    the session, then a ``session_files`` lookup verifies the
    ``file_key`` actually belongs to it. Refuses with 403 on the
    second check so a probe can't use someone else's file_key against
    their own session.
    """
    session = await _get_or_404(db, session_id, current_user.id)
    try:
        await assert_file_belongs_to_session(
            db, session_id=session.id, file_key=file_key,
        )
    except FileNotInSessionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="File is not part of this session",
        )
    url, expires_in = file_storage.generate_download_url(
        file_key=file_key, filename=filename,
    )
    return DownloadUrlResponse(
        download_url=url, file_key=file_key, expires_in=expires_in,
    )


# ---- per-message feedback (thumbs up / down + optional comment) ----------
#
# One vote per user per message, enforced by ``UNIQUE(message_id,
# user_id)``. PUT does upsert (rating + comment in one call); DELETE
# clears the row. ``GET /sessions/{id}`` already round-trips the
# current user's feedback via the LEFT JOIN in ``_load_messages``,
# so there's no separate read endpoint.


async def _resolve_assistant_message(
    db: AsyncSession, session_id: uuid.UUID, message_id: int, user_id: int,
) -> Message:
    """Verify session ownership + the message belongs to it AND is
    an assistant message. Two 404s and one 400 — the same shape we
    use elsewhere so we don't leak existence of unrelated rows."""
    await _get_or_404(db, session_id, user_id)
    stmt = (
        select(Message)
        .where(Message.id == message_id, Message.session_id == session_id)
    )
    message = (await db.execute(stmt)).scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.role != MessageRole.ASSISTANT:
        raise HTTPException(
            status_code=400,
            detail="Feedback is only supported on assistant messages",
        )
    return message


@router.put(
    "/sessions/{session_id}/messages/{message_id}/feedback",
    response_model=FeedbackResponse,
)
async def upsert_feedback(
    session_id: uuid.UUID,
    message_id: int,
    body: FeedbackUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Upsert the caller's feedback on an assistant message.

    PUT semantics — idempotent. Look up an existing row by
    ``(message_id, user_id)``: present → mutate fields, absent →
    insert a new row. ``UNIQUE(message_id, user_id)`` is the backstop
    if a concurrent request from the same user races us through the
    SELECT-then-INSERT window — the second one IntegrityErrors,
    which only happens if the frontend's submit-disable state has
    been bypassed (impossible from our own UI).
    """
    await _resolve_assistant_message(
        db, session_id, message_id, current_user.id,
    )
    # Schema validator already stripped + rejected blank — ``body.comment``
    # is either ``None`` or a non-empty trimmed string here.

    stmt = select(MessageFeedback).where(
        MessageFeedback.message_id == message_id,
        MessageFeedback.user_id == current_user.id,
    )
    feedback = (await db.execute(stmt)).scalar_one_or_none()
    if feedback is None:
        feedback = MessageFeedback(
            message_id=message_id,
            user_id=current_user.id,
            rating=body.rating,
            comment=body.comment,
        )
        db.add(feedback)
    else:
        feedback.rating = body.rating
        feedback.comment = body.comment
        # ``MessageFeedback.updated_at`` has ``onupdate=func.now()``;
        # SQLAlchemy fires it on real UPDATE flushes (which this is),
        # so no explicit bump needed here.
    await db.commit()
    await db.refresh(feedback)
    return FeedbackResponse.model_validate(feedback)


@router.delete(
    "/sessions/{session_id}/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_feedback(
    session_id: uuid.UUID,
    message_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Clear the caller's feedback. No-op if none exists."""
    await _resolve_assistant_message(
        db, session_id, message_id, current_user.id,
    )
    await db.execute(
        delete(MessageFeedback).where(
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == current_user.id,
        )
    )
    await db.commit()
