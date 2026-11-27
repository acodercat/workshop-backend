"""Request-body reading shared by the API surfaces: multipart forms, and
raw bodies with a size cap.

Lives at the surface-agnostic level (like ``app/api/dependencies.py``)
because a skill bundle arrives the same way whether a user or an
operator sends it, and the cap must be enforced the same way on both.

The form is read **inside the handler**, not through a declared
``UploadFile`` / ``Form`` parameter: FastAPI reads a declared body before
it resolves any dependency, so a declared one would take a whole bundle
from anyone, authenticated or not. :func:`multipart` runs after the
dependencies have, and :func:`upload_request_body` declares the body in
OpenAPI by hand, since no parameter does.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import FormData, UploadFile

from app.errors import AppError, SkillBundleTooLargeError

UPLOAD_FIELD = "file"
_READ_CHUNK_BYTES = 1024 * 1024


@asynccontextmanager
async def multipart(request: Request) -> AsyncIterator[FormData]:
    """The request's multipart form, one file at most, spooled by the parser and
    closed on exit. Text fields beside it are the parser's default allowance,
    as they were for a declared parameter."""
    async with request.form(max_files=1) as form:
        yield form


def uploaded_file(form: FormData) -> UploadFile:
    """The file in the field ``file``."""
    upload = form.get(UPLOAD_FIELD)
    if not isinstance(upload, UploadFile):
        raise RequestValidationError([_missing(UPLOAD_FIELD)])
    return upload


def form_field(form: FormData, name: str) -> str:
    """The text field ``name``."""
    value = form.get(name)
    if not isinstance(value, str):
        raise RequestValidationError([_missing(name)])
    return value


def _missing(field: str) -> dict[str, Any]:
    return {"type": "missing", "loc": ("body", field), "msg": "Field required"}


def upload_request_body(**fields: dict[str, Any]) -> dict[str, Any]:
    """The OpenAPI request body: the file, plus the named text fields (each a
    schema), all required."""
    return {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [UPLOAD_FIELD, *fields],
                    "properties": {
                        UPLOAD_FIELD: {"type": "string", "format": "binary"},
                        **fields,
                    },
                }
            }
        },
    }


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
            raise SkillBundleTooLargeError(f"upload exceeds the {max_bytes}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def read_capped_body(request: Request, max_bytes: int, *, too_large: AppError) -> bytes:
    """Read a raw request body into memory, refusing past ``max_bytes``.

    The declared ``Content-Length`` is checked first so an honest oversized
    request is refused before any of it is read; the stream is counted
    anyway, because the header is the client's claim.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise too_large
    return bytes(body)
