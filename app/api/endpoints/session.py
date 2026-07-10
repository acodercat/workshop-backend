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
from typing import Annotated, Literal

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
from app.common.file_kind import FILE_KINDS, file_kind_case
from app.common.filenames import InvalidFilenameError, filename_from_key
from app.common.pagination import paginate
from app.errors import (
    FileCountQuotaExceededError,
    FileSizeQuotaExceededError,
)
from app.models.message import Message, MessageRole
from app.models.message_feedback import MessageFeedback
from app.models.session import Session, SessionStatus
from app.models.session_files import SessionFile, SessionFileOrigin
from app.models.session_turn import TerminationReason
from app.models.session_share import SessionShare
from app.models.session_site import SessionSite
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.file import (
    DownloadUrlResponse,
    PreviewUrlResponse,
    SessionFileResponse,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.schemas.message_feedback import FeedbackResponse, FeedbackUpsert
from app.schemas.site import SessionSiteResponse
from app.services import file_storage
from app.services.file_storage import (
    SESSION_FILE_POLICY,
    FileTooLargeError,
    UnsupportedFileTypeError,
)
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
from app.services import credits
from app.services.redis import SESSION_STREAM_PREFIX, cancel_channel, stream_key
from app.services.session import generate_title, queue_user_message, stream_events
from app.services.session.exceptions import (
    ConcurrentTaskLimitError,
    FileNotInSessionError,
    GenerationInProgressError,
    RecipeNotInvokableError,
    SessionGoneError,
)
from app.services.session.files import assert_file_belongs_to_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sessions"])


async def _get_or_404(
    db: AsyncSession,
    session_id: str | uuid.UUID,
    user_id: int,
    *,
    for_update: bool = False,
) -> Session:
    """Return the session row iff it exists, is not soft-deleted, AND
    the caller owns it.

    404 (not 403) on an ownership miss so the existence of other users'
    session ids isn't leaked. ``deleted_at IS NULL`` is folded into the
    query so every endpoint funnelling through here treats a
    soft-deleted session as gone.

    ``for_update`` takes a row-level lock held until the transaction
    ends — used by ``delete_session`` so its status check and tombstone
    serialize against ``queue_user_message``'s own ``FOR UPDATE`` lock,
    closing the race where a turn enqueues onto a session being deleted.
    """
    stmt = select(Session).where(
        Session.id == session_id,
        Session.user_id == user_id,
        Session.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def _load_messages(
    db: AsyncSession, session_id: uuid.UUID, user_id: int,
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
        # Secondary sort on id: ``now()`` is transaction-scoped, so a USER
        # row and the worker's ASSISTANT row committed in one transaction
        # share a ``created_at`` — id breaks the tie in insert order.
        .order_by(Message.created_at.asc(), Message.id.asc())
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


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_session(
    request: Request,
    body: SessionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> SessionResponse:
    # The client picks a capability tier; resolve it to the concrete model
    # the worker / credits use. ``model_id`` is never sent over the wire.
    model_id = settings.session_tier_models.get(body.tier)
    if model_id is None or model_id not in settings.supported_session_model_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unconfigured tier: {body.tier!r}",
        )
    session = Session(
        title=body.title,
        user_id=current_user.id,
        model_id=model_id,
        tier=body.tier,
        agent_type=body.agent_type,
        thinking_enabled=body.thinking_enabled,
        status=SessionStatus.IDLE,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return SessionResponse.model_validate(session)


@router.get("/sessions")
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> PaginatedResponse[SessionResponse]:
    filter_clause = sa.and_(
        Session.user_id == current_user.id,
        Session.deleted_at.is_(None),
    )
    base = (
        select(Session)
        .where(filter_clause)
        .order_by(Session.updated_at.desc())
    )
    total = (
        await db.execute(select(func.count(Session.id)).where(filter_clause))
    ).scalar() or 0
    rows = (await db.execute(paginate(base, page, page_size))).scalars().all()
    return PaginatedResponse(
        items=[SessionResponse.model_validate(s) for s in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


# Registered BEFORE ``/sessions/{session_id}`` as a route-match-order
# guard. ``{session_id}`` is typed ``uuid.UUID`` now, so a literal like
# ``upload-url`` no longer validates as a session_id (it 422s instead of
# dispatching to a session handler) — but keeping the literal route first
# is the explicit, order-independent guarantee.
@router.post("/sessions/upload-url")
@limiter.limit(settings.rate_limit_upload_url)
async def create_session_upload_url(
    request: Request,
    body: UploadUrlRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> UploadUrlResponse:
    """Mint a presigned PUT URL for an attachment the user is about to
    send with a session message.

    Validates filename extension (whitelist in
    :mod:`app.services.file_storage`) and rejects oversize requests
    before the URL is issued. Also enforces a per-user upload quota
    (total size, with a file-count backstop) so a single account can't
    squat OSS by accumulating files across sessions. The eventual
    file→session binding happens when the message is enqueued
    (``services/session/enqueue.py`` inserts the ``session_files`` row);
    this endpoint is purely the "mint me a presigned URL" step.
    """
    await _enforce_session_file_quota(db, current_user.id, body.size_bytes)
    try:
        return file_storage.generate_upload_url(
            body.filename, body.size_bytes, SESSION_FILE_POLICY,
        )
    except (InvalidFilenameError, UnsupportedFileTypeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc),
        )


@router.get("/sessions/files")
async def list_files(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    origin: Literal["all", "upload", "deliverable"] = Query("all"),
    kind: str = Query("all"),
    search: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> PaginatedResponse[SessionFileResponse]:
    """List the caller's files across all their sessions, newest first.

    Declared before ``/sessions/{session_id}`` so the literal ``files``
    segment isn't parsed as a session id. Paginated server-side like
    ``GET /sessions``.

    Always scoped to the caller's non-deleted sessions and to user-facing
    files (``origin IN (UPLOAD, DELIVERABLE)`` — ``INTERNAL`` agent
    artifacts stay hidden). Optional filters, all applied server-side so
    paging stays correct: ``origin`` (``all`` / ``upload`` /
    ``deliverable``), ``kind`` (a file-type bucket classified from
    ``mime_type`` / ``filename`` via :func:`file_kind_case`), and
    ``search`` (substring match on filename or session title). ``filename``
    falls back to a key-derived name for any row not yet backfilled.
    """
    if kind != "all" and kind not in FILE_KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown kind: {kind!r}"
        )

    if origin == "upload":
        origin_clause = SessionFile.origin == SessionFileOrigin.UPLOAD
    elif origin == "deliverable":
        origin_clause = SessionFile.origin == SessionFileOrigin.DELIVERABLE
    else:
        origin_clause = SessionFile.origin.in_(
            (SessionFileOrigin.UPLOAD, SessionFileOrigin.DELIVERABLE)
        )

    kind_expr = file_kind_case(SessionFile.mime_type, SessionFile.filename)

    conditions = [
        Session.user_id == current_user.id,
        Session.deleted_at.is_(None),
        origin_clause,
    ]
    if kind != "all":
        conditions.append(kind_expr == kind)
    if search:
        # Escape LIKE wildcards so a literal "_" / "%" in the query (common
        # in filenames) matches literally rather than as a wildcard.
        escaped = (
            search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        conditions.append(
            sa.or_(
                SessionFile.filename.ilike(pattern, escape="\\"),
                Session.title.ilike(pattern, escape="\\"),
            )
        )
    filter_clause = sa.and_(*conditions)

    base = (
        select(
            SessionFile.file_key,
            SessionFile.filename,
            SessionFile.mime_type,
            SessionFile.size_bytes,
            SessionFile.created_at,
            SessionFile.origin,
            kind_expr.label("kind"),
            Session.id.label("session_id"),
            Session.title,
        )
        .join(Session, Session.id == SessionFile.session_id)
        .where(filter_clause)
        # Secondary sort on id keeps paging stable when timestamps tie.
        .order_by(SessionFile.created_at.desc(), SessionFile.id.desc())
    )
    total = (
        await db.execute(
            select(func.count(SessionFile.id))
            .join(Session, Session.id == SessionFile.session_id)
            .where(filter_clause)
        )
    ).scalar() or 0
    rows = (await db.execute(paginate(base, page, page_size))).all()
    return PaginatedResponse(
        items=[
            SessionFileResponse(
                file_key=row.file_key,
                filename=row.filename or filename_from_key(row.file_key),
                mime_type=row.mime_type,
                size_bytes=row.size_bytes,
                kind=row.kind,
                session_id=row.session_id,
                session_title=row.title,
                created_at=row.created_at,
                origin=row.origin,
            )
            for row in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.get("/sessions/sites")
async def list_sites(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> PaginatedResponse[SessionSiteResponse]:
    """List the caller's published sites across all their sessions, newest first.

    A sibling of ``GET /sessions/files`` for ``publish_site`` results.
    Declared before ``/sessions/{session_id}`` so the literal ``sites``
    segment isn't parsed as a session id. Paginated server-side like
    ``GET /sessions/files``.

    Scoped to the caller's non-deleted sessions. ``search`` is an optional
    substring match on the site title or its session title, applied
    server-side so paging stays correct. Each row's public ``url`` is
    derived from its ``slug`` and the configured preview domain.
    """
    conditions = [
        SessionSite.user_id == current_user.id,
        Session.deleted_at.is_(None),
    ]
    if search:
        # Escape LIKE wildcards so a literal "_" / "%" in the query matches
        # literally rather than as a wildcard (mirrors ``list_files``).
        escaped = (
            search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        conditions.append(
            sa.or_(
                SessionSite.title.ilike(pattern, escape="\\"),
                Session.title.ilike(pattern, escape="\\"),
            )
        )
    filter_clause = sa.and_(*conditions)

    base = (
        select(
            SessionSite.slug,
            SessionSite.title,
            SessionSite.created_at,
            Session.id.label("session_id"),
            Session.title.label("session_title"),
        )
        .join(Session, Session.id == SessionSite.session_id)
        .where(filter_clause)
        # Secondary sort on id keeps paging stable when timestamps tie.
        .order_by(SessionSite.created_at.desc(), SessionSite.id.desc())
    )
    total = (
        await db.execute(
            select(func.count(SessionSite.id))
            .join(Session, Session.id == SessionSite.session_id)
            .where(filter_clause)
        )
    ).scalar() or 0
    rows = (await db.execute(paginate(base, page, page_size))).all()
    preview_domain = settings.oss_preview_domain
    return PaginatedResponse(
        items=[
            SessionSiteResponse(
                url=f"https://{row.slug}.{preview_domain}/",
                slug=row.slug,
                title=row.title,
                session_id=row.session_id,
                session_title=row.session_title,
                created_at=row.created_at,
            )
            for row in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: uuid.UUID,
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
        tier=session.tier,
        agent_type=session.agent_type,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=messages,
    )


@router.patch("/sessions/{session_id}")
@limiter.limit("30/minute")
async def update_session(
    request: Request,
    session_id: uuid.UUID,
    body: SessionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> SessionResponse:
    session = await _get_or_404(db, session_id, current_user.id)
    if body.title is not None:
        session.title = body.title
    await db.commit()
    await db.refresh(session)
    return SessionResponse.model_validate(session)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_session(
    request: Request,
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Soft-delete a session (sets ``deleted_at``); 409 while RUNNING.

    The row + children are retained (credit_reservation back-link
    stays intact for billing/audit); the session just vanishes from
    the user's list and 404s on direct access. RUNNING is refused to
    avoid racing the worker — the caller retries once terminal.

    Any still-``OPEN`` reservation (a rare sweeper-lag artifact, since
    the session is terminal here) is closed by
    ``settle_open_for_session`` from durable ``session_turn`` usage —
    settle if the turn was used, refund if not, atomic with the
    tombstone. We do NOT blanket-refund: refunding a used turn just
    because the user tidied their list would be a revenue bug.

    Redis stream cleanup is best-effort; failure is logged, not
    raised — the worker's ``expire_stream`` TTL reaps the orphan.

    The row is locked (``for_update``) so the status check + tombstone is
    atomic against ``queue_user_message``: a concurrent enqueue either
    commits first (we then see ``RUNNING`` and 409) or blocks on our lock
    and finds the session gone afterward — never a turn on a deleted row.
    """
    session = await _get_or_404(
        db, session_id, current_user.id, for_update=True,
    )
    if session.status == SessionStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a session while a generation is in progress",
        )
    await credits.settle_open_for_session(db, session.id)
    session.deleted_at = func.now()
    await db.commit()

    try:
        await redis.delete(stream_key(SESSION_STREAM_PREFIX, str(session_id)))
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
    session_id: uuid.UUID,
    body: MessageCreate,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Create a user message and enqueue LLM generation to the worker.

    Service-layer domain errors translate to HTTP here so the service
    stays unaware of FastAPI: ``GenerationInProgressError`` → 409,
    ``ConcurrentTaskLimitError`` → 429 (user at their running-task cap),
    ``SessionGoneError`` → 404 (the session was deleted between our
    ownership check and the enqueue's row lock). The DB-lease design has
    no Redis-enqueue failure path: ``publish_wake`` is best-effort and
    never raises into this handler.
    """
    session = await _get_or_404(db, session_id, current_user.id)
    try:
        await queue_user_message(
            db, redis, session, content=body.content, files=body.files,
            skills=body.skills, recipe_id=body.recipe_id,
        )
    except GenerationInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        )
    except ConcurrentTaskLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc),
        )
    except SessionGoneError:
        raise HTTPException(status_code=404, detail="Session not found")
    except FileNotInSessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc),
        )
    except RecipeNotInvokableError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        )
    return MessageQueuedResponse()


@router.post(
    "/sessions/{session_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit("30/minute")
async def cancel_session(
    request: Request,
    session_id: uuid.UUID,
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

    The payload is the :class:`TerminationReason` the worker stamps on
    the turn — ``user_cancelled`` here; the budget enforcer publishes
    ``insufficient_credits`` on the same channel. The worker treats an
    empty/unknown payload as ``user_cancelled``, so the two sides can
    deploy independently.
    """
    session = await _get_or_404(db, session_id, current_user.id)
    if session.status != SessionStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session is not running",
        )
    await redis.publish(
        cancel_channel(str(session_id)), TerminationReason.USER_CANCELLED.value,
    )


@router.get("/sessions/{session_id}/stream")
@limiter.exempt
async def stream_session(
    session_id: uuid.UUID,
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
        stream_events(redis, str(session_id), last_event_id=last_event_id)
    )


@router.get(
    "/sessions/{session_id}/messages",
    response_model=list[MessageResponse],
    deprecated=True,
)
async def list_messages(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Deprecated — use ``GET /sessions/{id}`` for the structured payload."""
    await _get_or_404(db, session_id, current_user.id)
    return await _load_messages(db, session_id, current_user.id)


@router.post("/sessions/titles", response_model=TitleGenerateResponse)
@limiter.limit("10/minute")
async def generate_session_title(
    request: Request,
    body: TitleGenerateRequest,
    _user: User = Depends(authenticate_and_get_user),
) -> TitleGenerateResponse:
    """Stateless LLM-backed title generator for a conversation.

    Takes the user's first-message content and returns a short title.
    No session_id required and no DB writes — the frontend persists
    the result via ``PATCH /sessions/{id}`` once the session row
    exists. Splitting the two lets the sidebar show a real title as
    soon as the user hits send, without waiting for the agent turn.

    Auth: any logged-in user can call it. The ``_user`` dependency
    is what enforces that — without it, an anonymous attacker could
    burn the LLM's billing by spamming this endpoint. No further
    scoping is meaningful since no resource is read or written.
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


#
# Singleton sub-resource of the session: one share per session.
# Public-side read access for share recipients lives in
# :mod:`app.api.endpoints.share`; the two surfaces deliberately don't
# overlap on auth (user JWTs are rejected on the public path, share-
# view tokens are rejected here).


@router.post(
    "/sessions/{session_id}/share",
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("10/minute")
async def create_share(
    request: Request,
    session_id: uuid.UUID,
    body: ShareCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ShareResponse:
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
    return ShareResponse.model_validate(share)


@router.get("/sessions/{session_id}/share")
async def get_session_share(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ShareResponse:
    """Return the session's current share, or 404 if there isn't one."""
    await _get_or_404(db, session_id, current_user.id)

    stmt = select(SessionShare).where(SessionShare.session_id == session_id)
    share = (await db.execute(stmt)).scalar_one_or_none()
    if share is None:
        raise HTTPException(status_code=404, detail="Share not found")
    return ShareResponse.model_validate(share)


@router.delete(
    "/sessions/{session_id}/share",
    status_code=status.HTTP_204_NO_CONTENT,
)
@limiter.limit("30/minute")
async def delete_share(
    request: Request,
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


#
# Authorization is a single ``session_files`` lookup —
# ``(session_id, file_key)`` is the FK invariant set up at write time
# (user uploads in ``services/session/enqueue.py``, tool outputs in
# the worker before its SSE emit). Upload (``/sessions/upload-url``)
# lives at the top of this module.


@router.get(
    "/sessions/{session_id}/files/download-url",
    response_model=DownloadUrlResponse,
)
@limiter.limit("60/minute")
async def get_download_url(
    request: Request,
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
        file_storage.SESSION_FILE_POLICY, file_key, filename,
    )
    return DownloadUrlResponse(
        download_url=url, file_key=file_key, expires_in=expires_in,
    )


@router.get(
    "/sessions/{session_id}/files/preview-url",
    response_model=PreviewUrlResponse,
)
@limiter.limit("60/minute")
async def get_preview_url(
    request: Request,
    session_id: uuid.UUID,
    file_key: Annotated[str, Query(description="OSS file key of the HTML deliverable to preview")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
):
    """Mint a presigned GET URL that renders an HTML deliverable inline.

    Parallel to :func:`get_download_url`, but the minted URL carries
    ``inline`` disposition so it loads as a page inside an ``<iframe
    src>`` instead of triggering a save-as — the frontend uses it to
    preview HTML on the OSS origin, where its relative links can't
    navigate back into the app (the iframe-nesting fix). No ``filename``
    param: there's no save-as name for an inline render.

    ``preview-url`` is the resource being minted; ``file_key`` is a
    parameter to it (same path-vs-bytes reasoning as ``download-url``).
    Same two-step authorization as the download variant.
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
    url, expires_in = file_storage.generate_inline_url(
        file_storage.SESSION_FILE_POLICY, file_key,
    )
    return PreviewUrlResponse(
        preview_url=url, file_key=file_key, expires_in=expires_in,
    )


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
@limiter.limit("30/minute")
async def upsert_feedback(
    request: Request,
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
@limiter.limit("30/minute")
async def delete_feedback(
    request: Request,
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


async def _enforce_session_file_quota(
    db: AsyncSession, user_id: int, incoming_size_bytes: int
) -> None:
    """Reject the upload-url mint if this upload would put the user over a
    per-user quota. Two gates, both scoped to ``UPLOAD``-origin rows on
    non-soft-deleted sessions (deleting a session frees up quota):

    - **Total size** (primary): summed ``size_bytes`` plus this upload
      must stay under ``max_session_files_bytes_per_user``. This gate
      tracks real OSS storage cost.
    - **Count** (backstop): guards against many-tiny-files row explosion
      that the size gate alone wouldn't catch.

    Only ``UPLOAD`` rows count — files the user uploaded directly.
    ``DELIVERABLE`` (tool outputs) and ``INTERNAL`` (``read_image``
    inputs, office→PDF preview sidecars) are written by the worker with
    no upload-time gate, so counting them here would let heavy agent use
    silently exhaust a user's upload headroom.

    ``size_bytes`` is the client-declared size captured at mint time (the
    same value the per-file cap trusts); pre-backfill rows with a null
    size contribute 0 to the sum. Race-acceptable: two concurrent mints
    at the boundary can both pass, leaving the user slightly over — not
    worth a row lock for a soft policy limit.
    """
    count, used_bytes = (
        await db.execute(
            select(
                func.count(SessionFile.id),
                func.coalesce(func.sum(SessionFile.size_bytes), 0),
            )
            .join(Session, Session.id == SessionFile.session_id)
            .where(Session.user_id == user_id)
            .where(Session.deleted_at.is_(None))
            .where(SessionFile.origin == SessionFileOrigin.UPLOAD)
        )
    ).one()

    size_cap = settings.max_session_files_bytes_per_user
    if used_bytes + incoming_size_bytes > size_cap:
        raise FileSizeQuotaExceededError(
            f"upload would put the user at "
            f"{(used_bytes + incoming_size_bytes) / 1024**3:.2f} GB of "
            f"uploaded files; per-user limit is {size_cap / 1024**3:.1f} GB"
        )
    if count >= settings.max_session_files_per_user:
        raise FileCountQuotaExceededError(
            f"user already has {count} uploaded files; "
            f"per-user limit is {settings.max_session_files_per_user}"
        )
