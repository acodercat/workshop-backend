"""Public share endpoints — anonymous read access to a shared session.

Three routes, all under ``/shares/{token}``:

- ``GET  /shares/{token}`` — fetch the shared session content. Returns
  401 when the share is password-protected and the request lacks a
  valid view-token; the frontend treats any 401 from this route as
  "locked, prompt for password".
- ``POST /shares/{token}/access-tokens`` — exchange the share password
  for a short-lived view-token (rate-limited to slow brute-force).
- ``GET  /shares/{token}/files/{key}`` — mint a presigned OSS URL for a
  file the share's session owns (verified via ``session_files``).

Auth here uses **share-view tokens**, not user JWTs. The two token
classes are physically incompatible: share-view tokens carry
``scope=share-view`` which the user-token decoder rejects, and the
share decoder requires the same field. A user JWT cannot be used here,
and a share-view token cannot be used on the user-auth surface.

Owner-side share management (create / read / delete) lives next to
the rest of the session sub-resources in :mod:`session.py` —
``POST/GET/DELETE /sessions/{id}/share``.
"""

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import (
    create_share_view_token,
    decode_share_view_token,
    verify_password,
)
from app.models.message import Message
from app.models.session import Session
from app.models.session_share import SessionShare
from app.rate_limit import limiter
from app.schemas.file import DownloadUrlResponse
from app.schemas.session import MessageResponse
from app.schemas.session_share import (
    ShareAccessTokenRequest,
    ShareAccessTokenResponse,
    ShareDetailResponse,
)
from app.services import file_storage
from app.services.session.exceptions import FileNotInSessionError
from app.services.session.files import assert_file_belongs_to_session
from config import settings

router = APIRouter(tags=["shares"])

# ``auto_error=False`` because public (un-passworded) shares legitimately
# arrive without an ``Authorization`` header — we don't want FastAPI to
# 403 them on the way in. ``_require_view_access`` decides per-share
# whether missing credentials is fatal.
_share_view_bearer = HTTPBearer(auto_error=False)
_ShareViewCredentials = Annotated[
    HTTPAuthorizationCredentials | None, Depends(_share_view_bearer)
]


# ---- helpers --------------------------------------------------------------


async def _resolve_share(
    db: AsyncSession, token: str
) -> tuple[SessionShare, Session]:
    """Look up a share + its non-deleted session in one query. 404 on miss.

    Returns both rows so callers don't need a follow-up ``db.get``,
    and reading them together closes the TOCTOU window where the
    session could be deleted between two separate reads. The
    ``deleted_at IS NULL`` predicate revokes public access the moment
    the owner soft-deletes the session (soft-delete keeps the row, so
    the join would otherwise still resolve)."""
    stmt = (
        select(SessionShare, Session)
        .join(Session, Session.id == SessionShare.session_id)
        .where(
            SessionShare.token == token,
            Session.deleted_at.is_(None),
        )
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Share not found")
    return row[0], row[1]


def _locked_exception() -> HTTPException:
    """Build the 401 raised when a password-protected share is hit
    without a valid view token. ``WWW-Authenticate: Bearer`` mirrors
    the rest of the auth surface; the frontend distinguishes "this
    share is locked" by route + status alone, so the body is just the
    canonical ``{error, message}`` envelope without extras."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Share is password-protected.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _require_view_access(
    share: SessionShare,
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    """Gate: raise 401 if the share has a password and the request
    doesn't carry a valid view token bound to this share. Public
    (un-passworded) shares are always allowed."""
    if share.password_hash is None:
        return
    if credentials is None:
        raise _locked_exception()
    if decode_share_view_token(credentials.credentials) != share.token:
        # Bound to a different share, expired, or scope-mismatched.
        raise _locked_exception()


# ---- endpoints ------------------------------------------------------------


@router.get("/shares/{token}", response_model=ShareDetailResponse)
async def get_share(
    token: str,
    credentials: _ShareViewCredentials = None,
    db: AsyncSession = Depends(get_db),
):
    """Return the shared session's content.

    Auth model:

    - No password set on the share → 200 unconditionally.
    - Password set + valid ``Authorization: Bearer <view-token>`` → 200.
    - Password set + missing/invalid view token → 401. The frontend
      treats any 401 from this route as "locked, prompt for password"
      (route + status are sufficient discriminators).
    """
    share, session = await _resolve_share(db, token)
    await _require_view_access(share, credentials)

    messages_stmt = (
        select(Message)
        .where(Message.session_id == session.id)
        .order_by(Message.created_at.asc())
    )
    rows = (await db.execute(messages_stmt)).scalars().all()
    messages = [
        MessageResponse(
            id=row.id,
            role=row.role.value,
            blocks=row.blocks,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return ShareDetailResponse(
        id=session.id,
        title=session.title,
        status=session.status,
        created_at=session.created_at,
        messages=messages,
    )


@router.post(
    "/shares/{token}/access-tokens",
    response_model=ShareAccessTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(settings.rate_limit_share_unlock)
async def create_share_access_token(
    request: Request,
    token: str,
    body: ShareAccessTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a share's password for a short-lived view JWT.

    Per-IP rate limited to slow brute-force attempts. Hitting this
    endpoint on a public share returns 400 — public shares are
    directly readable via ``GET /shares/{token}`` without a token,
    so leaking that fact here doesn't change the attacker's options."""
    share, _ = await _resolve_share(db, token)
    if share.password_hash is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Share is public; no password required",
        )
    if not await verify_password(body.password, share.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
        )
    access_token, expires_at = create_share_view_token(share.token)
    return ShareAccessTokenResponse(
        access_token=access_token,
        expires_at=expires_at,
    )


@router.get(
    "/shares/{token}/files/download-url",
    response_model=DownloadUrlResponse,
)
@limiter.limit("60/minute")
async def get_share_file_url(
    request: Request,
    token: str,
    file_key: Annotated[str, Query(description="OSS file key returned from the upload step")],
    filename: Annotated[str, Query(description="Filename for Content-Disposition")],
    credentials: _ShareViewCredentials = None,
    db: AsyncSession = Depends(get_db),
) -> DownloadUrlResponse:
    """Mint a presigned download URL for a file the shared session owns.

    URL is intentionally **not** ``/files/{file_key}``: the response is
    a download-URL string, not the file's bytes. ``download-url`` is
    the resource being minted; ``file_key`` and ``filename`` are
    parameters to that minting.

    Authorization gate: same as :func:`get_share`. Additionally
    verifies via ``session_files`` that ``file_key`` is bound to
    ``share.session_id`` — without this, anyone holding a share token
    could use the share endpoint as an oracle for arbitrary OSS files.
    """
    share, session = await _resolve_share(db, token)
    await _require_view_access(share, credentials)
    try:
        await assert_file_belongs_to_session(
            db, session_id=share.session_id, file_key=file_key,
        )
    except FileNotInSessionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="File is not part of this shared session",
        )

    url, expires_in = file_storage.generate_download_url(
        file_key=file_key, filename=filename,
    )
    return DownloadUrlResponse(
        download_url=url, file_key=file_key, expires_in=expires_in,
    )
