"""Unified OSS storage — presigned URLs, uploads, downloads.

``UploadPolicy`` encapsulates per-asset-type config (bucket, extension
whitelist, size cap). ``generate_upload_url`` mints presigned PUT URLs
for session-file uploads; skill bundles upload directly via the
backend (multipart POST). Generic ops (``upload``, ``download``,
``head_size``, ``delete``) take a policy so callers never touch raw
bucket names.
"""

import os
import threading
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import oss2

from app.common.filenames import validate_input_filename
from app.schemas.file import UploadUrlResponse
from config import settings


class UnsupportedFileTypeError(ValueError):
    """Extension outside the policy's whitelist → HTTP 400."""

    def __init__(self, ext: str, allowed: list[str]) -> None:
        self.ext = ext
        self.allowed = allowed
        super().__init__(
            f"Unsupported file extension {ext!r}. Allowed: {', '.join(allowed)}"
        )


class FileTooLargeError(ValueError):
    """Declared size above the policy's cap → HTTP 413."""

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"File too large: {size_bytes} bytes exceeds the {max_bytes}-byte cap"
        )


UPLOAD_URL_TTL_SECONDS = 15 * 60
_DOWNLOAD_URL_TTL_SECONDS = 60 * 60


@dataclass(frozen=True)
class UploadPolicy:
    """Per-asset-type config scoping all OSS operations."""
    bucket_name: str
    key_prefix: str
    max_bytes: int
    allowed_types: dict[str, str]   # extension → content-type


# Session file uploads (any whitelisted type, 100 MiB cap). The OSS
# ``key_prefix`` matches the ``SessionFile`` DB model. Pre-existing
# objects keep their ``documents/...`` keys — file_storage operations
# read full keys from the DB and never parse the prefix, so the two
# prefixes coexist on the bucket without code changes.
SESSION_FILE_POLICY = UploadPolicy(
    bucket_name=settings.oss_document_bucket_name,
    key_prefix="session-files",
    max_bytes=settings.max_file_bytes,
    allowed_types={
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
        # images
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
        ".heic": "image/heic",
        ".heif": "image/heif",
        # video
        ".webm": "video/webm",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        # audio
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".opus": "audio/opus",
        ".weba": "audio/webm",
        ".wma": "audio/x-ms-wma",
    },
)

# Skill uploads (zip bundle or bare SKILL.md, 5 MiB cap, separate bucket).
SKILL_POLICY = UploadPolicy(
    bucket_name=settings.oss_skill_bucket_name,
    key_prefix="user_skills",
    max_bytes=5 * 1024 * 1024,
    allowed_types={
        ".zip": "application/zip",
        ".md": "text/markdown",
    },
)


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


def generate_upload_url(
    filename: str,
    size_bytes: int,
    policy: UploadPolicy,
    *,
    key_extra: str = "",
) -> UploadUrlResponse:
    """Validate + mint a presigned PUT URL per *policy*.

    ``key_extra`` is inserted between the policy prefix and the UUID —
    use it for per-user scoping (e.g. ``f"{user_id}/"``).
    """
    if size_bytes > policy.max_bytes:
        raise FileTooLargeError(size_bytes, policy.max_bytes)
    filename = validate_input_filename(filename)
    ext = os.path.splitext(filename)[1].lower()
    content_type = policy.allowed_types.get(ext)
    if content_type is None:
        raise UnsupportedFileTypeError(ext, sorted(policy.allowed_types))

    file_key = f"{policy.key_prefix}/{key_extra}{uuid.uuid4()}/{filename}"
    upload_url = get_bucket(policy.bucket_name).sign_url(
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


def download(policy: UploadPolicy, file_key: str) -> bytes:
    """Read an object's bytes into memory."""
    return get_bucket(policy.bucket_name).get_object(file_key).read()


def head_size(policy: UploadPolicy, file_key: str) -> int:
    """Return the actual byte size of an object in OSS."""
    return int(get_bucket(policy.bucket_name).head_object(file_key).content_length)


def upload(policy: UploadPolicy, file_key: str, data: bytes) -> None:
    """Write (or overwrite) an object in the policy's bucket."""
    get_bucket(policy.bucket_name).put_object(file_key, data)


def delete(policy: UploadPolicy, file_key: str) -> None:
    """Best-effort delete; OSS treats missing keys as no-ops."""
    get_bucket(policy.bucket_name).delete_object(file_key)


def generate_download_url(
    policy: UploadPolicy, file_key: str, filename: str,
) -> tuple[str, int]:
    """Presigned GET URL with Content-Disposition for browser save-as.

    ``filename`` is what the browser saves as — typically derived from
    a human-friendly identifier (skill slug, original document name)
    rather than the UUID-suffixed OSS key, which is meaningless to
    the user.
    """
    url = get_bucket(policy.bucket_name).sign_url(
        "GET",
        file_key,
        expires=_DOWNLOAD_URL_TTL_SECONDS,
        params={
            "response-content-disposition": (
                f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
            ),
        },
    )
    return url, _DOWNLOAD_URL_TTL_SECONDS
