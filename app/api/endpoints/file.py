"""HTTP adapter for the cross-session file listing.

A user's files — both their uploads and the files LLM tool calls
produce — are bound to their sessions through ``session_files``
(file_key ↔ session, the download-authorization table). ``GET /files``
reads that table across all the caller's sessions so a user's whole
file library can be listed in one place, newest first, without walking
each session's ``GET /sessions/{id}`` payload. Auth-scoped to the
caller, the same way ``GET /sessions`` is.

Each row carries its own ``filename`` / ``mime_type`` / ``size_bytes``
(denormalized from the message blocks); ``filename`` falls back to a
key-derived name for any row not yet backfilled. There is deliberately no
type filter — file kind isn't a column, so the client classifies by the
filename extension instead.

Download reuses the existing presigned-URL endpoint
(``GET /sessions/{id}/files/download-url``): a list item carries the
``session_id`` + ``file_key`` + ``filename`` that endpoint needs.
"""

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.common.filenames import filename_from_key
from app.common.pagination import paginate
from app.models.session import Session
from app.models.session_files import SessionFile, SessionFileOrigin
from app.models.user import User
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.file import SessionFileResponse

router = APIRouter(tags=["files"])


@router.get("/files")
async def list_files(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> PaginatedResponse[SessionFileResponse]:
    """List the caller's files across all their sessions, newest first.

    Joins ``session_files`` to ``session`` for the title and ownership;
    ``deleted_at IS NULL`` is folded in so a soft-deleted session's
    files vanish with it. Paged like ``GET /sessions``.

    Only user-facing files are listed — uploads and tool deliverables
    (``origin IN (UPLOAD, DELIVERABLE)``). Agent-side artifacts —
    ``read_image`` inputs the model looked at, the office→PDF preview
    sidecars — carry ``origin = INTERNAL`` and are hidden.
    """
    filter_clause = sa.and_(
        Session.user_id == current_user.id,
        Session.deleted_at.is_(None),
        SessionFile.origin.in_(
            (SessionFileOrigin.UPLOAD, SessionFileOrigin.DELIVERABLE)
        ),
    )
    base = (
        select(
            SessionFile.file_key,
            SessionFile.filename,
            SessionFile.mime_type,
            SessionFile.size_bytes,
            SessionFile.created_at,
            SessionFile.origin,
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
                session_id=row.session_id,
                session_title=row.title,
                created_at=row.created_at,
                origin=row.origin,
            )
            for row in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )
