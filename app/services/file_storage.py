"""OSS presigned-URL service — mints short-lived upload/download URLs.

The frontend uploads bytes directly to OSS using a presigned PUT URL
issued here, then posts only the resulting ``file_key`` back to
``/sessions/{id}/messages``. Downloads follow the same pattern in
reverse. The backend never proxies file bytes.
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


# Hard upload cap. Frontend echoes a friendlier message before issuing the
# request; this is the server-side guard against forged calls. 100 MiB =
# 100 * 1024 * 1024 to match the canonical "binary MB" used by browsers.
MAX_UPLOAD_BYTES = MAX_FILE_BYTES


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
_USER_SKILLS_OSS_PREFIX = "user_skills"

# Presigned URL lifetimes — fixed server-side. We don't let the client
# pick because a long-lived URL is a token the user can't revoke.
_UPLOAD_URL_TTL_SECONDS = 15 * 60
_DOWNLOAD_URL_TTL_SECONDS = 60 * 60

# Skill bundles are .zip only. The cap is much tighter than documents
# (5 MiB vs 100 MiB) because a skill is supposed to be a manifest +
# small helper scripts; big assets should ride as session inputs, not
# get baked into a skill.
SKILL_BUNDLE_MAX_BYTES = 5 * 1024 * 1024
_SKILL_BUNDLE_CONTENT_TYPE = "application/zip"
_SKILL_BUNDLE_EXTENSION = ".zip"


# Lazy-initialized one ``oss2.Bucket`` per logical bucket per process —
# the SDK object is thread-safe and expensive to rebuild on every
# request. Documents and user skills live in separate buckets so a
# leaked credential / lifecycle policy on one doesn't blast-radius
# the other.
_buckets: dict[str, oss2.Bucket] = {}
_buckets_lock = threading.Lock()


def _get_bucket(bucket_name: str) -> oss2.Bucket:
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
    return _get_bucket(settings.oss_document_bucket_name)


def _user_skills_bucket() -> oss2.Bucket:
    return _get_bucket(settings.oss_user_skill_bucket_name)


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
        expires=_UPLOAD_URL_TTL_SECONDS,
        headers={"Content-Type": content_type},
    )
    return UploadUrlResponse(
        upload_url=upload_url,
        file_key=file_key,
        content_type=content_type,
        expires_in=_UPLOAD_URL_TTL_SECONDS,
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


# ── Skill bundles ─────────────────────────────────────────────────────────
# Separate code path from the documents flow because the validation
# surface is different (zip-only, tighter cap, user-scoped key prefix
# for audit / cleanup) — folding it into ``generate_upload_url`` would
# need three new optional params and obscure both call sites.


def generate_skill_upload_url(
    filename: str, size_bytes: int, user_id: int,
) -> UploadUrlResponse:
    """Mint a presigned PUT URL for a user-uploaded skill bundle (.zip).

    ``user_id`` is baked into the OSS key for audit + per-user cleanup;
    UUID inside ensures concurrent uploads of identical filenames don't
    collide. Caller must already have authenticated the user (this fn
    trusts ``user_id`` as-given).
    """
    if size_bytes > SKILL_BUNDLE_MAX_BYTES:
        raise FileTooLargeError(size_bytes, SKILL_BUNDLE_MAX_BYTES)
    filename = validate_input_filename(filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext != _SKILL_BUNDLE_EXTENSION:
        raise UnsupportedFileTypeError(ext, [_SKILL_BUNDLE_EXTENSION])

    file_key = f"{_USER_SKILLS_OSS_PREFIX}/{user_id}/{uuid.uuid4()}/{filename}"
    upload_url = _user_skills_bucket().sign_url(
        "PUT",
        file_key,
        expires=_UPLOAD_URL_TTL_SECONDS,
        headers={"Content-Type": _SKILL_BUNDLE_CONTENT_TYPE},
    )
    return UploadUrlResponse(
        upload_url=upload_url,
        file_key=file_key,
        content_type=_SKILL_BUNDLE_CONTENT_TYPE,
        expires_in=_UPLOAD_URL_TTL_SECONDS,
    )


# Sync OSS ops below — caller wraps in ``asyncio.to_thread`` from an
# async context to avoid blocking the event loop on the network round-trip.


def download_skill_bundle(file_key: str) -> bytes:
    """Read a skill bundle's bytes into memory for SKILL.md parsing."""
    return _user_skills_bucket().get_object(file_key).read()


def head_skill_bundle_size(file_key: str) -> int:
    """Actual byte size of an uploaded skill bundle in OSS.

    Used at register time to re-check the cap — OSS doesn't honor a
    signed ``Content-Length`` so the upload-URL size declaration is
    advisory; a misbehaving client can PUT more bytes than declared.
    """
    return int(_user_skills_bucket().head_object(file_key).content_length)


def delete_skill_bundle(file_key: str) -> None:
    """Best-effort delete; OSS treats missing keys as no-ops on DELETE."""
    _user_skills_bucket().delete_object(file_key)
