"""File upload/download presigned URL endpoints.

The frontend exchanges bytes with OSS directly via presigned URLs;
the backend never proxies file content.

**Authorization model**: both endpoints require a valid JWT (via
:func:`authenticate_and_get_user`). Beyond that, we do *not* check
whether the caller "owns" a given ``file_key`` — the file_key itself
is UUID-backed and practically unguessable (``documents/{uuid4}/…``
for user uploads, ``outputs/{session_uuid}/{tool_use_id}/…`` for
tool outputs), and the presigned URL's 60-minute TTL is the final
capability boundary. Tying the download endpoint to ``Message``/
``Session`` tables added coupling (and a timing hole during live
streaming, since tool outputs reach the SSE stream before the DB
commit at end-of-turn) without meaningful defense-in-depth beyond
what the key entropy and TTL already provide.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import authenticate_and_get_user
from app.models.user import User
from app.schemas.file import DownloadUrlResponse, UploadUrlResponse
from app.services import file_storage
from app.services.file_storage import FileTooLargeError, UnsupportedFileTypeError

router = APIRouter(tags=["files"])


@router.get("/files/upload-url", response_model=UploadUrlResponse)
async def get_upload_url(
    filename: str = Query(..., description="Name of the file to upload"),
    size_bytes: int = Query(..., ge=0, description="Declared file size in bytes"),
    _user: User = Depends(authenticate_and_get_user),
):
    """Mint a presigned PUT URL for the frontend to upload a document to OSS.

    Validates the filename's extension against the backend whitelist and
    rejects oversize requests (server-side cap, see
    :data:`file_storage.MAX_UPLOAD_BYTES`) before the URL is issued.
    """
    try:
        return file_storage.generate_upload_url(
            filename=filename, size_bytes=size_bytes,
        )
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc),
        )


@router.get("/files/download-url", response_model=DownloadUrlResponse)
async def get_download_url(
    file_key: str = Query(..., description="OSS file key returned from the upload step"),
    filename: str = Query(..., description="Original filename for Content-Disposition"),
    _user: User = Depends(authenticate_and_get_user),
):
    """Mint a presigned GET URL for a previously-minted file_key.

    Any authenticated caller who knows the key can fetch one — see the
    module docstring for why this is the right authorization boundary.
    """
    url, expires_in = file_storage.generate_download_url(
        file_key=file_key, filename=filename,
    )
    return DownloadUrlResponse(download_url=url, file_key=file_key, expires_in=expires_in)
