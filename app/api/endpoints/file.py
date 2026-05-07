"""File upload endpoint — presigned PUT URL for OSS.

Upload is session-agnostic: the frontend may collect a file key from
this endpoint before knowing which session (or whether) it'll
reference the file. Validation happens at upload time (extension
whitelist + server-side size cap); the file→session binding is
recorded later by the message-create path inserting a ``session_files``
row in the same transaction as the user message.

Download is **not** here — it's session-bound (authorization is a
single ``session_files`` lookup) and lives in :mod:`session` alongside
the rest of the owner-side surface.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.auth import authenticate_and_get_user
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.file import UploadUrlResponse
from app.services import file_storage
from app.services.file_storage import FileTooLargeError, UnsupportedFileTypeError

router = APIRouter(tags=["files"])


@router.get("/files/upload-url", response_model=UploadUrlResponse)
@limiter.limit("60/minute")
async def get_upload_url(
    request: Request,
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
