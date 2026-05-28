"""Filename validation shared by upload and session file references.

User-uploaded files are staged by agent-worker under a fixed sandbox
input directory. Keep backend validation aligned with the worker so a
file accepted here cannot fail later because its name escapes that
directory.
"""

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
