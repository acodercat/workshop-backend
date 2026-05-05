"""Schemas for the file-upload / download presigned-URL endpoints."""

from pydantic import BaseModel


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
