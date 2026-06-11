"""Schemas for the file upload / download / listing endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.session_files import SessionFileOrigin


class UploadUrlRequest(BaseModel):
    """Body for POST endpoints that mint presigned upload URLs."""
    filename: str = Field(..., description="Name of the file to upload")
    size_bytes: int = Field(..., ge=0, description="Declared file size in bytes")


class UploadUrlResponse(BaseModel):
    """Presigned URL for the frontend to PUT file bytes directly into OSS."""
    upload_url: str
    file_key: str
    content_type: str
    expires_in: int


class DownloadUrlResponse(BaseModel):
    """Presigned URL the frontend uses to download a file the user previously uploaded."""
    download_url: str
    file_key: str
    expires_in: int


class SessionFileResponse(BaseModel):
    """A row in the cross-session file library. ``filename`` falls back to
    a key-derived name for rows whose stored metadata isn't backfilled.
    """
    file_key: str
    filename: str
    mime_type: str | None
    size_bytes: int | None
    session_id: UUID
    session_title: str | None
    created_at: datetime
    # UPLOAD (user-attached) vs DELIVERABLE (agent-produced) — drives the
    # source filter in the library. INTERNAL never reaches here; the
    # endpoint filters it out.
    origin: SessionFileOrigin
