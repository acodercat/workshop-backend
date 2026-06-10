"""Schemas for the file upload / download / listing endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


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
    """One file in the cross-session ``GET /files`` listing.

    A ``session_files`` row joined to its owning session. ``filename``
    is derived from the OSS key — the table stores no metadata (the
    presentation copy lives on the message blocks). Pair ``session_id``
    + ``file_key`` with ``GET /sessions/{id}/files/download-url`` to
    fetch the bytes.
    """
    file_key: str
    filename: str
    session_id: UUID
    session_title: str | None
    created_at: datetime
