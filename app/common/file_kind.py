"""Server-side file-type (``kind``) classification for the file library.

The library's type filter buckets a file into one coarse kind (image /
video / audio / web / spreadsheet / presentation / document / archive /
code / other).
Classifying in the query — off the existing ``mime_type`` / ``filename``
columns — lets ``GET /sessions/files`` filter by kind server-side (so
pagination stays correct) without a stored ``kind`` column. This is the
single source of the rule; the frontend renders whatever kind the API
returns rather than re-deriving it.

The rules are a plain table — ``(kind, mime keywords, filename
extensions)`` — and :func:`file_kind_case` compiles them into one SQL
``CASE``. Add a type or an extension by editing :data:`_KIND_RULES`.
"""

from sqlalchemy import ColumnElement, case, func, or_

# Ordered most-specific → most-general; the first matching row wins. A row
# matches when the (lower-cased) mime type *contains* any of its keywords,
# or the filename ends in any of its extensions. Unmatched → ``other``.
_KIND_RULES: list[tuple[str, list[str], list[str]]] = [
    ("image", ["image/"], ["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "heic", "heif"]),
    ("video", ["video/"], ["webm", "mp4", "mov", "m4v", "mkv", "avi"]),
    (
        "audio",
        ["audio/"],
        ["mp3", "wav", "ogg", "oga", "m4a", "aac", "flac", "opus", "weba", "wma"],
    ),
    ("web", ["text/html"], ["html", "htm"]),
    ("spreadsheet", ["csv", "spreadsheet", "excel"], ["xls", "xlsx", "csv"]),
    ("presentation", ["presentation", "powerpoint"], ["ppt", "pptx", "pps", "ppsx"]),
    # ``text/plain`` is deliberately omitted as a mime keyword — code, CSV
    # and log files are all served as text/plain and would be stolen from
    # later rules (``code`` especially). ``.txt`` still maps here by extension.
    (
        "document",
        ["pdf", "wordprocessing", "msword", "markdown", "rtf"],
        ["pdf", "doc", "docx", "md", "markdown", "txt", "rtf"],
    ),
    (
        "archive",
        ["zip", "compressed", "tar", "gzip", "rar"],
        ["zip", "rar", "7z", "tar", "gz", "tgz"],
    ),
    (
        "code",
        ["json", "xml", "x-python", "javascript", "typescript", "yaml", "sql", "x-sh", "x-ipynb"],
        [
            "py",
            "ipynb",
            "js",
            "jsx",
            "ts",
            "tsx",
            "mjs",
            "cjs",
            "sh",
            "bash",
            "zsh",
            "json",
            "yaml",
            "yml",
            "toml",
            "xml",
            "sql",
            "css",
        ],
    ),
]

# Kinds the classifier can emit — the rule kinds plus the ``other`` fallback.
# The listing endpoint validates its ``kind`` filter against this so an
# unknown bucket 422s instead of silently matching nothing.
FILE_KINDS: frozenset[str] = frozenset(kind for kind, _, _ in _KIND_RULES) | {"other"}


def file_kind_case(
    mime_type: ColumnElement[str | None],
    filename: ColumnElement[str | None],
) -> ColumnElement[str]:
    """Compile :data:`_KIND_RULES` into a SQL ``CASE`` → a kind bucket.

    Reused in ``SELECT`` (labelled ``kind``) and in ``WHERE`` (``== :kind``)
    so the listed and the filtered kind can never disagree.
    """
    mime = func.lower(func.coalesce(mime_type, ""))
    name = func.lower(func.coalesce(filename, ""))
    branches = []
    for kind, mime_keywords, extensions in _KIND_RULES:
        predicates = [mime.contains(keyword) for keyword in mime_keywords]
        if extensions:
            predicates.append(name.op("~")(rf"\.({'|'.join(extensions)})$"))
        branches.append((or_(*predicates), kind))
    return case(*branches, else_="other")
