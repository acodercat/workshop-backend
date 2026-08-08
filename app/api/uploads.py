"""Multipart reading shared by both surfaces.

Lives at the surface-agnostic level (like ``app/api/dependencies.py``)
because a skill bundle arrives the same way whether a user or an
operator sends it, and the cap must be enforced the same way on both.
"""

from fastapi import UploadFile

from app.errors import SkillBundleTooLargeError

_READ_CHUNK_BYTES = 1024 * 1024


async def read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read an ``UploadFile`` into memory, aborting past ``max_bytes``.

    ``UploadFile.read()`` with no argument loads the entire (possibly
    disk-spooled) body into one ``bytes`` — for a hostile multi-GB upload
    that is the OOM. Reading in bounded chunks and bailing the instant the
    running total exceeds the cap keeps peak memory at ``max_bytes`` + one
    chunk. The registration service re-checks the same cap on the returned
    bytes, so this is purely the early-abort guard.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise SkillBundleTooLargeError(
                f"upload exceeds the {max_bytes}-byte limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)
