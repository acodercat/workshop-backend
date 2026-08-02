"""One-off: fill metadata for the few visible files the blocks backfill
couldn't reach, by HEAD-ing the object in OSS.

``scripts/backfill_session_file_metadata.py`` lifts filename / mime_type /
size_bytes off ``message.blocks``, but a handful of old DELIVERABLE rows
have no block entry (they predate file-metadata-in-blocks). For those OSS
is the only remaining source of truth: a HEAD gives content-length (size)
and content-type (mime); the filename comes from the key.

Targets visible rows (UPLOAD / DELIVERABLE) still missing a filename.
Objects that have been deleted from OSS are skipped. Idempotent.

    uv run python -m scripts.archive.backfill_session_file_metadata_from_oss
"""

import asyncio
import mimetypes
import os

import oss2
from sqlalchemy import text

from app.common.filenames import filename_from_key
from app.database import AsyncSessionLocal
from app.services.file_storage import SESSION_FILE_POLICY, get_bucket


def _head(bucket: oss2.Bucket, file_key: str) -> tuple[int, str | None] | None:
    """``(size_bytes, content_type)`` for the object, or ``None`` if it is
    gone / unreadable in OSS."""
    try:
        meta = bucket.head_object(file_key)
    except oss2.exceptions.OssError:
        return None
    return int(meta.content_length), meta.content_type


def _resolve_mime(content_type: str | None, filename: str) -> str | None:
    """Prefer the stored OSS content-type; fall back to the extension when
    it is missing or the generic ``application/octet-stream``."""
    if content_type and content_type != "application/octet-stream":
        return content_type
    ext = os.path.splitext(filename)[1].lower()
    return SESSION_FILE_POLICY.allowed_types.get(ext) or mimetypes.guess_type(
        filename
    )[0]


async def main() -> None:
    bucket = get_bucket(SESSION_FILE_POLICY.bucket_name)
    async with AsyncSessionLocal() as db:
        keys = [
            key
            for (key,) in (
                await db.execute(
                    text(
                        "SELECT file_key FROM session_files "
                        "WHERE origin IN ('UPLOAD', 'DELIVERABLE') "
                        "AND filename IS NULL"
                    )
                )
            ).all()
        ]
        print(f"{len(keys)} visible rows missing metadata")

        filled = 0
        gone = 0
        for file_key in keys:
            head = await asyncio.to_thread(_head, bucket, file_key)
            if head is None:
                gone += 1
                continue
            size_bytes, content_type = head
            filename = filename_from_key(file_key)
            await db.execute(
                text(
                    "UPDATE session_files SET "
                    "filename = :filename, mime_type = :mime, size_bytes = :size "
                    "WHERE file_key = :key AND filename IS NULL"
                ),
                {
                    "filename": filename,
                    "mime": _resolve_mime(content_type, filename),
                    "size": size_bytes,
                    "key": file_key,
                },
            )
            filled += 1
        await db.commit()
        print(f"filled {filled}, skipped {gone} (object gone from OSS)")


if __name__ == "__main__":
    asyncio.run(main())
