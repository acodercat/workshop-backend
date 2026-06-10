"""Filename helpers shared across upload, session-file, and listing code.

``validate_input_filename`` guards user-supplied names — uploads are
staged by agent-worker under a fixed sandbox input directory, so a name
accepted here must not later escape it. ``filename_from_key`` recovers a
display name from an OSS key for rows that store no filename of their own.
"""

import re
from pathlib import PurePosixPath


class InvalidFilenameError(ValueError):
    """Raised when a user-supplied filename is not a safe basename."""


def validate_input_filename(filename: object) -> str:
    """Return ``filename`` iff it is a safe single path component.

    Rejects absolute paths, nested paths, dot components, Windows-style
    separators, NUL bytes, and non-strings. The returned string is safe
    to append below the worker sandbox input directory.
    """
    if not isinstance(filename, str):
        raise InvalidFilenameError(f"Invalid filename: {filename!r}")

    path = PurePosixPath(filename)
    if (
        not filename
        or filename in {".", ".."}
        or path.is_absolute()
        or path.name != filename
        or "\\" in filename
        or "\0" in filename
    ):
        raise InvalidFilenameError(f"Invalid filename: {filename!r}")
    return filename


_OUTPUT_INDEX_PREFIX = re.compile(r"^\d+_")


def filename_from_key(file_key: str) -> str:
    """Recover a display filename from an OSS key, for rows that store no
    filename of their own.

    The name is the key's last segment — the original filename for uploads
    (``session-files/{uuid}/{name}``, legacy ``documents/...``). Tool
    outputs (``outputs/{sid}/{tool_use_id}/{idx}_{name}``) additionally
    carry the result-index prefix, dropped here.
    """
    name = file_key.rsplit("/", 1)[-1]
    if file_key.startswith("outputs/"):
        name = _OUTPUT_INDEX_PREFIX.sub("", name, count=1)
    return name
