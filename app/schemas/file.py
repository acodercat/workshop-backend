"""Schemas for the file-upload / download presigned-URL endpoints."""

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
