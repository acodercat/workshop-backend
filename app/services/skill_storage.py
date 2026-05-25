"""OSS storage for user-uploaded SKILL.md bundles.

Sibling to :mod:`app.services.file_storage`. The two share the OSS
bucket factory + URL TTL + size/type errors (imported below) but live
as separate modules because the domain shape differs: separate bucket,
.zip-only, 5 MiB cap, user-scoped key prefix, and three extra ops the
documents flow doesn't need (head/download/delete for the register +
delete-skill endpoint).
"""

import os
import uuid

from app.common.filenames import validate_input_filename
from app.schemas.file import UploadUrlResponse
from app.services.file_storage import (
    FileTooLargeError,
    UPLOAD_URL_TTL_SECONDS,
    UnsupportedFileTypeError,
    get_bucket,
)
from config import settings


# 5 MiB. A skill bundle is supposed to be SKILL.md + small helper
# scripts; big assets should ride as session inputs, not get baked into
# a skill. Public — :mod:`app.api.endpoints.skill` re-checks against
# this at register time.
SKILL_BUNDLE_MAX_BYTES = 5 * 1024 * 1024

_OSS_PREFIX = "user_skills"
_BUNDLE_CONTENT_TYPE = "application/zip"
_BUNDLE_EXTENSION = ".zip"


def _bucket():
    return get_bucket(settings.oss_skill_bucket_name)


def user_key_prefix(user_id: int) -> str:
    """Common OSS-key prefix for every bundle owned by ``user_id``.

    Both the URL minter and the register endpoint's auth check
    construct/verify keys; this is the one place that knows the shape.
    """
    return f"{_OSS_PREFIX}/{user_id}/"


def generate_upload_url(
    filename: str, size_bytes: int, user_id: int,
) -> UploadUrlResponse:
    """Mint a presigned PUT URL for a user-uploaded skill bundle (.zip).

    The OSS key (``user_skills/{user_id}/{uuid}/{filename}``) bakes
    in ``user_id`` for audit + per-user cleanup; the UUID makes
    concurrent uploads of identical filenames non-colliding. Caller
    must already have authenticated the user.
    """
    if size_bytes > SKILL_BUNDLE_MAX_BYTES:
        raise FileTooLargeError(size_bytes, SKILL_BUNDLE_MAX_BYTES)
    filename = validate_input_filename(filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext != _BUNDLE_EXTENSION:
        raise UnsupportedFileTypeError(ext, [_BUNDLE_EXTENSION])

    file_key = f"{user_key_prefix(user_id)}{uuid.uuid4()}/{filename}"
    upload_url = _bucket().sign_url(
        "PUT",
        file_key,
        expires=UPLOAD_URL_TTL_SECONDS,
        headers={"Content-Type": _BUNDLE_CONTENT_TYPE},
    )
    return UploadUrlResponse(
        upload_url=upload_url,
        file_key=file_key,
        content_type=_BUNDLE_CONTENT_TYPE,
        expires_in=UPLOAD_URL_TTL_SECONDS,
    )


# Sync OSS ops below — caller wraps in ``asyncio.to_thread`` from an
# async context to avoid blocking the event loop on the network round-trip.


def download_bundle(file_key: str) -> bytes:
    """Read a skill bundle's bytes into memory for SKILL.md parsing."""
    return _bucket().get_object(file_key).read()


def head_bundle_size(file_key: str) -> int:
    """Actual byte size of an uploaded skill bundle in OSS.

    Used at register time to re-check the cap *before* downloading —
    OSS doesn't honor a signed ``Content-Length`` so the upload-URL
    declaration is advisory; a misbehaving client can PUT past it.
    HEAD is a cheap (~100ms, no body) probe so an oversize bundle is
    rejected before it ever lands in backend RAM.
    """
    return int(_bucket().head_object(file_key).content_length)


def delete_bundle(file_key: str) -> None:
    """Best-effort delete; OSS treats missing keys as no-ops on DELETE."""
    _bucket().delete_object(file_key)
