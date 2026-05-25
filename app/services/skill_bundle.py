"""Skill bundle validation + SKILL.md frontmatter extraction.

A skill bundle is a .zip whose contents follow the axon SKILL convention:
a ``SKILL.md`` at root (or at the single top-level directory's root —
the common "github download" wrapper) plus optional ``scripts/`` /
``references/`` / ``assets/`` siblings.

This module is the **registration-time** validator: it answers
"is this zip a legal skill bundle and what's its name + description?"
so :mod:`app.api.endpoints.skill` can decide to write the row.
Asset content is **not** scanned here — the worker stages files into
the sandbox at session start and any per-file issue surfaces there.

Threats this layer is responsible for:
- path traversal in zip entries (``..`` or absolute paths)
- symlink entries (refuse — Python's ``zipfile`` doesn't follow them,
  but staging code might)
- missing or unparseable ``SKILL.md`` (would silently produce a
  no-op skill the LLM can never use)
- absurd nesting / oversized inner files (caller already enforces
  the wrapped zip's overall size cap at upload time)
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

# Mirrors :func:`axon.skills._parse_frontmatter` — flat ``key: value``
# lines only between the leading and trailing ``---`` fences. Anything
# fancier is silently dropped (kept consistent with axon's behavior
# so what backend extracts == what worker eventually re-discovers).
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)---\s*\n?", re.DOTALL)
_SKILL_FILENAME = "SKILL.md"

# Picker label budgets — match the DB column lengths in
# :class:`app.models.skill.Skill`. We truncate (silently, with
# an ellipsis) rather than reject so a verbose description doesn't
# block registration on a one-line cosmetic issue.
_MAX_NAME_CHARS = 64
_MAX_DESCRIPTION_CHARS = 512


class InvalidSkillBundleError(ValueError):
    """Bundle failed validation — the endpoint maps this to HTTP 400.

    Message is operator-facing and end-user-presentable (the frontend
    surfaces it in a toast), so phrase reasons concretely (e.g.
    "SKILL.md not found in bundle") rather than as stack-trace fragments.
    """


@dataclass(frozen=True)
class ParsedSkill:
    """Metadata extracted from a validated bundle's ``SKILL.md``."""
    name: str
    description: str


def parse_bundle(data: bytes) -> ParsedSkill:
    """Validate ``data`` is a legal skill bundle and return its metadata.

    Raises :class:`InvalidSkillBundleError` on any structural problem.
    Does **not** persist anything — pure function over bytes.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InvalidSkillBundleError(f"not a valid zip file: {exc}") from exc

    with zf:
        _reject_unsafe_entries(zf)
        skill_md_path = _locate_skill_md(zf)
        try:
            raw = zf.read(skill_md_path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidSkillBundleError(
                f"SKILL.md is not valid UTF-8: {exc}",
            ) from exc

    meta, body = _split_frontmatter(raw)
    # Folder name fallback for the github-download wrapper case
    # (``my-skill-main/SKILL.md`` → ``my-skill-main``); empty if
    # SKILL.md is at the root.
    folder_fallback = skill_md_path.rsplit("/", 1)[0] if "/" in skill_md_path else ""

    name = _clip(meta.get("name") or folder_fallback or "skill", _MAX_NAME_CHARS)
    description = _clip(
        meta.get("description") or _first_nonempty_line(body) or name,
        _MAX_DESCRIPTION_CHARS,
    )
    return ParsedSkill(name=name, description=description)


def _reject_unsafe_entries(zf: zipfile.ZipFile) -> None:
    """Guard against path traversal and symlinks.

    Symlinks are encoded in zip's external attrs (Unix file type bits);
    Python's ``zipfile`` extracts them as regular files but downstream
    consumers (the worker's staging step) may invoke an unzip CLI that
    DOES follow them — reject defensively here so the contract is
    consistent regardless of who does the actual extraction later.
    """
    for info in zf.infolist():
        # Path traversal: ``..`` segments or absolute paths
        name = info.filename
        if not name or name.startswith("/") or "\\" in name:
            raise InvalidSkillBundleError(
                f"refused unsafe path in bundle: {name!r}",
            )
        # Normalize and check for parent escapes
        parts = name.split("/")
        if any(p == ".." for p in parts):
            raise InvalidSkillBundleError(
                f"refused parent-traversal path: {name!r}",
            )
        # Symlink detection: Unix file type S_IFLNK = 0o120000 in upper
        # 16 bits of external_attr when ``create_system == 3`` (Unix).
        if info.create_system == 3 and (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise InvalidSkillBundleError(
                f"refused symlink entry in bundle: {name!r}",
            )


def _locate_skill_md(zf: zipfile.ZipFile) -> str:
    """Find ``SKILL.md`` at root or at the only top-level subdirectory.

    Matches the GitHub-download wrapper case where everything sits
    under ``<repo>-main/``. Rejects multiple top-level directories so
    one bundle == one skill (mirrors the worker's per-directory
    discovery).
    """
    names = zf.namelist()

    # Pass 1: SKILL.md at root
    if _SKILL_FILENAME in names:
        return _SKILL_FILENAME

    # Pass 2: SKILL.md inside exactly one top-level directory
    top_levels = {n.split("/", 1)[0] for n in names if "/" in n}
    # Drop empty / pure-root entries; some zips include a ``"/"`` entry
    top_levels = {t for t in top_levels if t}
    candidates = [f"{t}/{_SKILL_FILENAME}" for t in top_levels]
    matches = [c for c in candidates if c in names]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise InvalidSkillBundleError(
            "bundle contains multiple SKILL.md files; expected exactly one",
        )

    raise InvalidSkillBundleError("SKILL.md not found in bundle")


def _split_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Same tiny YAML subset as axon's ``_parse_frontmatter``."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        if value:
            meta[key.strip()] = value.strip()
    return meta, content[match.end():]


def _first_nonempty_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
