"""OSS presigned-URL service for **user-uploaded session documents**.

The frontend uploads bytes directly to OSS using a presigned PUT URL
issued here, then posts only the resulting ``file_key`` back to
``/sessions/{id}/messages``. Downloads follow the same pattern in
reverse. The backend never proxies file bytes.

Skill-bundle storage lives in :mod:`app.services.skill_storage` — same
OSS infrastructure (the bucket factory + URL TTL + the error classes
defined below are imported and reused), but a different bucket /
content-type whitelist / size cap / key prefix that wouldn't survive
being folded into one ``generate_upload_url`` function.
"""

import os
import threading
import uuid
from urllib.parse import quote

import oss2

from app.common.file_limits import MAX_FILE_BYTES
from app.common.filenames import validate_input_filename
from app.schemas.file import UploadUrlResponse
from config import settings


class UnsupportedFileTypeError(ValueError):
    """Raised when an upload request names an extension outside the whitelist.

    Domain exception — endpoints translate to HTTP 400. The service
    layer doesn't know it's being called from FastAPI.
    """

    def __init__(self, ext: str, allowed: list[str]) -> None:
        self.ext = ext
        self.allowed = allowed
        super().__init__(
            f"Unsupported file extension {ext!r}. Allowed: {', '.join(allowed)}"
        )


class FileTooLargeError(ValueError):
    """Raised when an upload request declares a size above the cap."""

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"File too large: {size_bytes} bytes exceeds the {max_bytes}-byte cap"
        )


# Hard upload cap for documents. Frontend echoes a friendlier message
# before issuing the request; this is the server-side guard against
# forged calls. 100 MiB to match the canonical "binary MB" browsers use.
MAX_UPLOAD_BYTES = MAX_FILE_BYTES


# Presigned URL lifetimes — fixed server-side. We don't let the client
# pick because a long-lived URL is a token the user can't revoke.
UPLOAD_URL_TTL_SECONDS = 15 * 60
_DOWNLOAD_URL_TTL_SECONDS = 60 * 60


# Extension → content-type whitelist for user document uploads.
# Add new extensions here; anything else is rejected at the upload-URL
# step so the frontend can't ask OSS to store arbitrary file types.
_DOCUMENT_CONTENT_TYPES: dict[str, str] = {
    # documents
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    # spreadsheets
    ".csv": "text/csv",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # data / config
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".ipynb": "application/x-ipynb+json",
    # code
    ".py": "text/x-python",
    # archives
    ".zip": "application/zip",
    ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    # images — common web formats + iPhone HEIC. SVG is whitelisted
    # despite being able to embed JS because uploads are scoped to
    # authenticated users in this internal tool; if the threat model
    # changes, drop it from this list.
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

_DOCUMENT_OSS_PREFIX = "documents"


# ── Shared OSS bucket factory ────────────────────────────────────────────
# Lazy-initialized one ``oss2.Bucket`` per logical bucket per process.
# The SDK object is thread-safe and expensive to rebuild on every
# request. Documents and user skills live in separate buckets so a
# leaked credential / lifecycle policy on one doesn't blast-radius
# the other.
_buckets: dict[str, oss2.Bucket] = {}
_buckets_lock = threading.Lock()


def get_bucket(bucket_name: str) -> oss2.Bucket:
    bucket = _buckets.get(bucket_name)
    if bucket is None:
        with _buckets_lock:
            bucket = _buckets.get(bucket_name)
            if bucket is None:
                auth = oss2.Auth(
                    settings.oss_access_key_id,
                    settings.oss_access_key_secret,
                )
                bucket = oss2.Bucket(auth, settings.oss_endpoint, bucket_name)
                _buckets[bucket_name] = bucket
    return bucket


def _documents_bucket() -> oss2.Bucket:
    return get_bucket(settings.oss_document_bucket_name)


# ── Documents: presigned PUT / GET ──────────────────────────────────────


def generate_upload_url(filename: str, size_bytes: int) -> UploadUrlResponse:
    """Validate ``filename``/``size_bytes`` and return a presigned PUT URL.

    The signed URL bakes in ``Content-Type`` only — OSS doesn't honor a
    signed ``Content-Length``, so the size cap is enforced at this
    layer (declared by the client) rather than at PUT time. A client
    that lies about ``size_bytes`` and uploads more bytes will succeed
    against OSS, so the cap is anti-mistake, not anti-malice — pair it
    with bucket-level lifecycle rules if you need a hard ceiling.
    """
    if size_bytes > MAX_UPLOAD_BYTES:
        raise FileTooLargeError(size_bytes, MAX_UPLOAD_BYTES)
    filename = validate_input_filename(filename)
    content_type = _content_type_for(filename)
    file_key = f"{_DOCUMENT_OSS_PREFIX}/{uuid.uuid4()}/{filename}"

    upload_url = _documents_bucket().sign_url(
        "PUT",
        file_key,
        expires=UPLOAD_URL_TTL_SECONDS,
        headers={"Content-Type": content_type},
    )
    return UploadUrlResponse(
        upload_url=upload_url,
        file_key=file_key,
        content_type=content_type,
        expires_in=UPLOAD_URL_TTL_SECONDS,
    )


def generate_download_url(file_key: str, filename: str) -> tuple[str, int]:
    """Return ``(presigned GET URL, expires_in_seconds)`` for the given key.

    ``filename`` rides along as a ``response-content-disposition``
    override baked into the signed URL, so OSS responds with
    ``Content-Disposition: attachment; filename*=UTF-8''…``. The
    browser's ``<a download>`` attribute is ignored on cross-origin
    URLs, so this header is the only way to make the file save with
    its real name instead of the OSS path with slashes replaced by
    underscores.
    """
    url = _documents_bucket().sign_url(
        "GET",
        file_key,
        expires=_DOWNLOAD_URL_TTL_SECONDS,
        params={
            "response-content-disposition": _attachment_disposition(filename),
        },
    )
    return url, _DOWNLOAD_URL_TTL_SECONDS


def _attachment_disposition(filename: str) -> str:
    """RFC 5987 ``Content-Disposition: attachment`` for any Unicode name."""
    return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"


def _content_type_for(filename: str) -> str:
    """Return the MIME type for a supported extension.

    Raises :class:`UnsupportedFileTypeError` for anything outside the
    whitelist — the endpoint translates that into HTTP 400.
    """
    ext = os.path.splitext(filename)[1].lower()
    content_type = _DOCUMENT_CONTENT_TYPES.get(ext)
    if content_type is None:
        raise UnsupportedFileTypeError(ext, sorted(_DOCUMENT_CONTENT_TYPES))
    return content_type
