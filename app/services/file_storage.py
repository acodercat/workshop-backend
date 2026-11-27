"""Unified OSS storage — presigned URLs, uploads, downloads.

``UploadPolicy`` encapsulates per-asset-type config (bucket, extension →
Content-Type map, size cap). ``generate_upload_url`` mints presigned PUT URLs
for session-file uploads; skill bundles upload directly via the
backend (multipart POST). Generic ops (``upload``, ``download``,
``head_size``, ``delete``) take a policy so callers never touch raw
bucket names.
"""

import asyncio
import os
import threading
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import oss2

from app.common.filenames import validate_input_filename
from app.errors import FileNotUploadedError, FileRetentionFailedError, FileTooLargeError
from app.schemas.file import UploadUrlResponse
from config import settings


class FileObjectNotFoundError(ValueError):
    """The OSS object doesn't exist (e.g. a presigned upload never
    happened). Translates oss2's ``NoSuchKey`` at the storage boundary so
    callers don't import ``oss2``. ``confirmed_sizes`` turns it into
    :class:`~app.errors.FileNotUploadedError` at bind time.
    """

    def __init__(self, file_key: str) -> None:
        self.file_key = file_key
        super().__init__(f"No OSS object for key {file_key!r}")


class InlinePreviewNotAllowedError(ValueError):
    """File type may not be served with an ``inline`` disposition → HTTP 415.

    Inline preview renders the object as active content on the OSS
    document origin; a script-bearing ``.svg`` would be stored XSS there.
    The client should use the download URL instead.
    """

    def __init__(self, ext: str) -> None:
        self.ext = ext
        super().__init__(f"{ext!r} cannot be previewed inline; download it instead")


# The only extensions that may be served with an ``inline`` disposition.
#
# An allow-list, and it has to be: uploads accept any extension now, so a
# deny-list would have to enumerate every scriptable type the browser
# might ever execute on the document origin (``.html``, ``.svg``,
# ``.xhtml``, ``.xml``, ``.mjs``, whatever ships next) and would be one
# omission away from stored XSS on our own domain. Inverting it means a
# type nobody vetted downloads instead of rendering — the safe default,
# and the only thing that changes for the user is one click.
#
# ``.html`` is here because it *is* the intended payload: agent-produced
# web deliverables render in an iframe, which is what this whole path
# exists for. It is also the reason the list can't simply be "images":
# raster images are inert, but HTML is not, and it is trusted here
# because the worker wrote it — not because someone uploaded it.
_INLINE_ALLOWED_EXTENSIONS = frozenset(
    {".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
)


UPLOAD_URL_TTL_SECONDS = 15 * 60
_DOWNLOAD_URL_TTL_SECONDS = 60 * 60

# Orphan-reaping tag (M5). A presigned upload is born tagged
# ``lifecycle=orphan``; a bucket lifecycle rule deletes objects still
# carrying it after ~1 day, and :func:`clear_orphan_tag` removes it when the
# upload is bound to a session. ``ORPHAN_TAGGING_HEADER`` is the OSS
# url-encoded ``key=value`` tagging-header value the presigned PUT signs and
# the client must echo. Gated by ``settings.oss_orphan_tagging_enabled``.
_ORPHAN_TAG_KEY = "lifecycle"
_ORPHAN_TAG_VALUE = "orphan"
ORPHAN_TAGGING_HEADER = f"{_ORPHAN_TAG_KEY}={_ORPHAN_TAG_VALUE}"


@dataclass(frozen=True)
class UploadPolicy:
    """Per-asset-type config scoping all OSS operations."""

    bucket_name: str
    key_prefix: str
    max_bytes: int
    # extension → content-type. For session files this is a *lookup table*,
    # not a gate: an extension missing from it uploads as
    # ``application/octet-stream``. ``SKILL_POLICY`` still treats it as an
    # allow-list — see ``skill_registration._canonical_ext``.
    allowed_types: dict[str, str]


# Session file uploads (any extension, 100 MiB cap). The OSS
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

# Meeting recordings uploaded to be handed to Meeting. Kept only until the
# archiver has put them into Meeting's own storage, or the recording fails
# for good; deleted either way. Unlike session files, ``allowed_types`` is
# an allow-list here (``recordings.media_extension`` enforces it): Meeting
# transcribes audio and video, nothing else.
MEETING_MEDIA_POLICY = UploadPolicy(
    bucket_name=settings.oss_document_bucket_name,
    key_prefix="meeting-media",
    max_bytes=settings.meeting_max_upload_bytes,
    allowed_types={
        ".mp3": "audio/mpeg",
        ".mpga": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".webm": "video/webm",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mpeg": "video/mpeg",
    },
)

# Skill uploads (zip bundle or bare SKILL.md, separate bucket). 50 MiB cap
# matches the Agent Skills standard's maximum upload size.
SKILL_POLICY = UploadPolicy(
    bucket_name=settings.oss_skill_bucket_name,
    key_prefix="user_skills",
    max_bytes=50 * 1024 * 1024,
    allowed_types={
        ".zip": "application/zip",
        ".skill": "application/zip",  # a ``.skill`` is a renamed zip (portable Agent Skills package)
        ".md": "text/markdown",
    },
)


_buckets: dict[tuple[str, str | None], oss2.Bucket] = {}
_buckets_lock = threading.Lock()


def get_bucket(bucket_name: str, *, cname_domain: str | None = None) -> oss2.Bucket:
    """Process-wide cached bucket handle.

    ``cname_domain`` signs URLs against a bound custom domain (CNAME)
    instead of the default OSS endpoint — needed for inline previews,
    since the default ``*.aliyuncs.com`` endpoint force-downloads HTML in
    mainland regions. ``None`` uses the default endpoint (every caller but
    the inline-preview one). Cached per ``(bucket, cname_domain)``."""
    key = (bucket_name, cname_domain)
    bucket = _buckets.get(key)
    if bucket is None:
        with _buckets_lock:
            bucket = _buckets.get(key)
            if bucket is None:
                auth = oss2.Auth(
                    settings.oss_access_key_id,
                    settings.oss_access_key_secret,
                )
                bucket = (
                    oss2.Bucket(auth, cname_domain, bucket_name, is_cname=True)
                    if cname_domain
                    else oss2.Bucket(auth, settings.oss_endpoint, bucket_name)
                )
                _buckets[key] = bucket
    return bucket


def generate_upload_url(
    filename: str,
    size_bytes: int,
    policy: UploadPolicy,
) -> UploadUrlResponse:
    """Validate + mint a presigned PUT URL per *policy*."""
    if size_bytes > policy.max_bytes:
        raise FileTooLargeError(size_bytes, policy.max_bytes)
    filename = validate_input_filename(filename)
    ext = os.path.splitext(filename)[1].lower()
    # Any extension uploads. The table still supplies a precise
    # Content-Type where we know one — it is signed into the URL and
    # becomes the object's stored type, which is what later decides
    # whether a browser renders or downloads it — and everything else
    # gets the inert generic. Nothing about "can this be uploaded"
    # depends on the extension any more; what a file is *allowed to do*
    # afterwards is decided at the point of use (see
    # ``_INLINE_ALLOWED_EXTENSIONS`` for the inline-preview gate).
    content_type = policy.allowed_types.get(ext, "application/octet-stream")

    file_key = f"{policy.key_prefix}/{uuid.uuid4()}/{filename}"
    # Tag the object orphan at upload (M5) so a never-bound upload is reaped
    # by the bucket lifecycle rule. Signed into the URL, so the client must
    # echo the header exactly — ``tagging_header`` tells it what to send, and
    # is omitted (no signed header) when the feature is off, keeping the
    # client in lockstep per request.
    sign_headers = {"Content-Type": content_type}
    tagging_header: str | None = None
    if settings.oss_orphan_tagging_enabled:
        tagging_header = ORPHAN_TAGGING_HEADER
        sign_headers["x-oss-tagging"] = tagging_header
    upload_url = get_bucket(policy.bucket_name).sign_url(
        "PUT",
        file_key,
        expires=UPLOAD_URL_TTL_SECONDS,
        headers=sign_headers,
    )
    return UploadUrlResponse(
        upload_url=upload_url,
        file_key=file_key,
        content_type=content_type,
        expires_in=UPLOAD_URL_TTL_SECONDS,
        tagging_header=tagging_header,
    )


def clear_orphan_tag(policy: UploadPolicy, file_key: str) -> None:
    """Remove the orphan lifecycle tag so the object survives the reaper.

    Called when a presigned upload is bound to a session. Idempotent: OSS
    treats deleting tags on an untagged object as a no-op, so it's safe for
    objects uploaded before the feature was enabled (or already cleared).
    """
    get_bucket(policy.bucket_name).delete_object_tagging(file_key)


async def confirmed_sizes(policy: UploadPolicy, file_keys: list[str]) -> list[int]:
    """Each key's true size in OSS, in the order given.

    The one place a persisted ``size_bytes`` becomes trustworthy. A client's
    declared size is a claim, and a low one slips past both the per-file cap
    and the per-user quota, which sums the column — so every write path that
    stores a size reconciles it here rather than each re-deriving how.

    Raises :class:`FileNotUploadedError` for a key with no object — the
    presigned upload never completed — and :class:`FileTooLargeError` for one
    over the policy's cap. Both are typed application errors, so every caller
    can let them propagate: that "no object" means "not uploaded" is decided
    once, here, rather than re-translated at each write path.

    Blocking oss2, threaded and fanned out, so N keys cost about one HEAD's
    latency rather than N serial ones. A single failure is raised rather than
    collected: none of the callers can store a partial set.
    """
    if not file_keys:
        return []
    sizes = await asyncio.gather(
        *(asyncio.to_thread(head_size, policy, key) for key in file_keys),
        return_exceptions=True,
    )
    confirmed: list[int] = []
    for size in sizes:
        if isinstance(size, FileObjectNotFoundError):
            raise FileNotUploadedError(f"file_key {size.file_key!r} was not uploaded") from size
        if isinstance(size, BaseException):
            raise size
        if size > policy.max_bytes:
            raise FileTooLargeError(size, policy.max_bytes)
        confirmed.append(size)
    return confirmed


async def clear_orphan_tags(policy: UploadPolicy, file_keys: list[str]) -> None:
    """Take uploads off the orphan-reaping rule, or raise
    :class:`FileRetentionFailedError`.

    Whether a failure may be ignored is the caller's call, not this
    function's: a message's attachments are bound and read within the turn,
    so its caller logs and moves on, while a project file must survive until
    some session reads it, so its caller refuses the add. Raising lets both
    say so.

    Every key is attempted before anything is raised, so a retry has only
    what actually failed left to do — the call is idempotent. A no-op when
    tagging is off, because then nothing was tagged. Blocking oss2, threaded
    and fanned out, so N keys cost about one round trip.
    """
    if not settings.oss_orphan_tagging_enabled or not file_keys:
        return
    results = await asyncio.gather(
        *(asyncio.to_thread(clear_orphan_tag, policy, key) for key in file_keys),
        return_exceptions=True,
    )
    failed = [
        (key, result) for key, result in zip(file_keys, results) if isinstance(result, Exception)
    ]
    if failed:
        key, first = failed[0]
        raise FileRetentionFailedError(
            f"Could not clear the orphan tag on {len(failed)} of {len(file_keys)} "
            f"file(s), starting with {key!r}"
        ) from first


def download(policy: UploadPolicy, file_key: str) -> bytes:
    """Read an object's bytes into memory."""
    return get_bucket(policy.bucket_name).get_object(file_key).read()


def head_size(policy: UploadPolicy, file_key: str) -> int:
    """Return the actual byte size of an object in OSS.

    Raises :class:`FileObjectNotFoundError` if the object doesn't exist
    (a presigned upload that never happened) — the caller reconciles the
    trusted-at-mint client size against this real size. Blocking oss2
    call; run it off the event loop (``asyncio.to_thread``).
    """
    try:
        head = get_bucket(policy.bucket_name).head_object(file_key)
    except oss2.exceptions.NoSuchBucket:
        # Also a ``NotFound``, and not the caller's to handle: a missing
        # bucket is a deployment fault, not a missing upload.
        raise
    except oss2.exceptions.NotFound as exc:
        # ``NotFound`` rather than only ``NoSuchKey``: a HEAD 404 arrives with
        # no body, so oss2 can only name ``NoSuchKey`` when the ``x-oss-err``
        # header is present — and it is not always. ``NoSuchKey`` is a
        # subclass, so this catches both spellings of "no such object".
        raise FileObjectNotFoundError(file_key) from exc
    return int(head.content_length)


def upload(policy: UploadPolicy, file_key: str, data: bytes) -> None:
    """Write (or overwrite) an object in the policy's bucket."""
    get_bucket(policy.bucket_name).put_object(file_key, data)


def delete(policy: UploadPolicy, file_key: str) -> None:
    """Best-effort delete; OSS treats missing keys as no-ops."""
    get_bucket(policy.bucket_name).delete_object(file_key)


def generate_download_url(
    policy: UploadPolicy,
    file_key: str,
    filename: str,
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


def generate_inline_url(policy: UploadPolicy, file_key: str) -> tuple[str, int]:
    """Presigned GET URL the browser renders **inline** — for HTML previews.

    The mirror image of :func:`generate_download_url`: instead of forcing
    ``attachment`` (save-as), it signs ``inline`` so the object loads as a
    page inside an ``<iframe src>``. Serving it from the OSS origin — not
    the app's — is the whole point: relative links in the delivered HTML
    resolve against OSS, so the page can't navigate the frame back into
    the app and render the app nested inside itself.

    We deliberately do NOT override the response ``Content-Type``: Aliyun
    OSS rejects ``response-content-type`` on a presigned GET ("Can not
    override response header on content-type", an XSS-hardening rule), so
    rendering relies on the object's *stored* Content-Type — delivered
    ``.html`` is uploaded as ``text/html``. HTML-only by design; binary
    previews (PDF / Office) fetch bytes through their own viewer and
    ignore Content-Disposition, so they keep using the download URL.

    Signs against ``oss_document_domain`` (a bound custom domain) when set
    — required because the default ``*.aliyuncs.com`` endpoint
    force-downloads HTML in mainland regions regardless of the ``inline``
    disposition. Falls back to the default endpoint when unset (the URL
    then downloads, but nothing breaks).

    Raises :class:`InlinePreviewNotAllowedError` for anything outside
    ``_INLINE_ALLOWED_EXTENSIONS`` — serving an unvetted type inline on the
    document origin would be stored XSS. The single choke point for both
    the session and share preview endpoints, so neither can mint an inline
    URL for one.
    """
    ext = os.path.splitext(file_key)[1].lower()
    if ext not in _INLINE_ALLOWED_EXTENSIONS:
        raise InlinePreviewNotAllowedError(ext)
    url = get_bucket(policy.bucket_name, cname_domain=settings.oss_document_domain).sign_url(
        "GET",
        file_key,
        expires=_DOWNLOAD_URL_TTL_SECONDS,
        params={"response-content-disposition": "inline"},
    )
    return url, _DOWNLOAD_URL_TTL_SECONDS
