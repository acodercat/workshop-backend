"""Skill bundle validation + SKILL.md frontmatter extraction.

A skill bundle is a .zip whose contents follow the axon SKILL convention:
a ``SKILL.md`` at root (or at the single top-level directory's root —
the common "github download" wrapper) plus optional ``scripts/`` /
``references/`` / ``assets/`` siblings.

This module is the **registration-time** validator: it answers
"is this zip a legal skill bundle and what's its name + description?"
so :mod:`app.api.public.skill` can decide to write the row.

Refusals raise :mod:`app.errors` types directly, as every other service
does — and a different type per thing the uploader would do next, so the
client can say which one without reading English out of ``message``.
Everything an uploader cannot act on (a zip bomb, a path that escapes its
archive) stays under the one generic ``InvalidSkillBundleError``.
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
from typing import Any

from app.errors import (
    InvalidSkillBundleError,
    InvalidSkillNameError,
    SkillBodyTooLargeError,
    SkillBundleTooLargeError,
    SkillBundleUnreadableError,
    SkillFrontmatterInvalidError,
    SkillMdMissingError,
)
from app.services.skill_i18n import parse_display_name

# Bounds the ``---`` … ``---`` frontmatter block; the body between the
# fences is parsed with ``yaml.safe_load`` (see ``_split_frontmatter``).
# Mirrors :func:`axon.skills._parse_frontmatter`, which uses the same regex
# + ``yaml.safe_load``, so what the backend extracts at upload matches what
# the worker re-discovers from the staged bundle.
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)^---[ \t]*\r?\n?", re.DOTALL | re.MULTILINE)
_SKILL_FILENAME = "SKILL.md"

# Picker label budgets — match the DB column lengths in
# :class:`app.models.skill.Skill`. Skill names are identifiers, so reject
# invalid values. Descriptions are display text, so a verbose value is clipped.
_MAX_NAME_CHARS = 64
_MAX_DESCRIPTION_CHARS = 1024
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# SKILL.md body (everything after the frontmatter) is injected into the
# agent's context when the skill is invoked and kept through compaction
# (axon marks it ``preserve_in_compact``), so its size is a permanent
# context-budget cost. Anthropic's Agent Skills guidance is < ~5000 tokens
# / < 500 lines, with bulk content split into reference files the agent
# loads on demand. 32 KiB (~8K tokens) is a generous cap above that.
_MAX_BODY_BYTES = 32 * 1024  # 32 KiB

# Decompression-safety / distribution limits. The per-file size and file
# count match the Agent Skills standard (≤ 25 MB uncompressed per file,
# ≤ 500 files); the wrapper zip itself is capped at upload time
# (``SKILL_POLICY.max_bytes`` = 50 MiB). The compression-ratio guard is
# ours — a real bundle compresses ~3-10×, so > 100× is a zip bomb. The
# ratio catches "many small entries that cumulatively explode"; the
# absolute total cap is the hard ceiling that ratio alone misses — a
# ratio-100 archive of 50 MiB still declares ~5 GB of expansion, which
# ``normalize_bundle`` would actually materialize in memory while
# repacking. Both guards are header-only (no bytes decompressed), so a
# bundle that declares more than the total cap is rejected before any
# allocation. 200 MiB is generous headroom over the 500 × 25 MiB real
# ceiling while still bounding the repack's peak memory.
_MAX_UNCOMPRESSED_PER_ENTRY = 25 * 1024 * 1024  # 25 MiB — Agent Skills: per file
_MAX_ENTRY_COUNT = 500                          # Agent Skills: files per bundle
_MAX_COMPRESSION_RATIO = 100
_MAX_TOTAL_UNCOMPRESSED = 200 * 1024 * 1024     # 200 MiB — hard ceiling on declared expansion


@dataclass(frozen=True)
class ParsedSkill:
    """Metadata extracted from a validated bundle's ``SKILL.md``."""
    name: str
    display_name: str
    display_name_i18n: dict[str, str] | None
    description: str


def validate_zip_structure(data: bytes) -> None:
    """Reject malformed / hostile archives before any decompression.

    Run this **first**, before :func:`normalize_bundle` or
    :func:`parse_bundle` — both of those touch entry bytes (the former
    by repacking, the latter by reading SKILL.md), so an unguarded
    zip-bomb here would let the attacker inflate memory before the
    structural checks fire.

    Pure header-only inspection — reads the central directory, never
    a compressed stream. Raises :class:`InvalidSkillBundleError` on
    bad-zip / path-traversal / symlink / zip-bomb signals.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillBundleUnreadableError(f"not a valid zip file: {exc}") from exc

    with zf:
        _reject_unsafe_entries(zf)
        _reject_zip_bomb(zf)


def parse_bundle(data: bytes) -> ParsedSkill:
    """Extract metadata from a structurally-validated skill bundle.

    Pre-condition: caller has already run :func:`validate_zip_structure`.
    Raises :class:`InvalidSkillBundleError` if SKILL.md is missing or
    malformed. Does **not** persist anything — pure function over bytes.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillBundleUnreadableError(f"not a valid zip file: {exc}") from exc

    with zf:
        raw = _decode_skill_md(zf.read(_locate_skill_md(zf)))
    return _parse_skill_md_text(raw)


def normalize_bundle(data: bytes) -> bytes | None:
    """If the zip has a single wrapper directory, repack with SKILL.md at root.

    Pre-condition: caller has already run :func:`validate_zip_structure`.
    This function decompresses every entry to repack it; calling it on
    an unvalidated archive would let a zip bomb inflate memory before
    any size check ran.

    Returns the repacked bytes if flattened, or ``None`` if the bundle
    is already in root-level format (no re-upload needed). This ensures
    all bundles in OSS have a consistent layout regardless of how the
    user zipped them, so the worker and frontend never need to handle
    wrapper dirs.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None

    with zf:
        # Check if SKILL.md is already at root
        if _SKILL_FILENAME in zf.namelist():
            return None

        # Detect single wrapper directory
        top_levels = {n.split("/", 1)[0] for n in zf.namelist() if "/" in n}
        top_levels = {t for t in top_levels if t}
        root_files = {n for n in zf.namelist() if "/" not in n}
        if len(top_levels) != 1 or root_files:
            return None

        wrapper = next(iter(top_levels))
        prefix = f"{wrapper}/"

        # Repack: strip the wrapper prefix from all entries
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as out:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                new_name = info.filename.removeprefix(prefix)
                out.writestr(new_name, zf.read(info.filename))

    return buf.getvalue()


def parse_skill_md(data: bytes) -> ParsedSkill:
    """Validate ``data`` as a bare SKILL.md file and return its metadata.

    Same validation as the zip path but without any archive handling — the
    bytes ARE the SKILL.md content.
    """
    return _parse_skill_md_text(_decode_skill_md(data))


def _reject_zip_bomb(zf: zipfile.ZipFile) -> None:
    """Reject archives whose declared expansion is unreasonable.

    Header-only — reads the zip central directory, never decompresses a
    byte (the reported sizes are what an unzipper would allocate up front).
    Three checks: file count and per-entry uncompressed size (the Agent
    Skills distribution limits), plus a compression-ratio guard that catches
    many small entries each under the per-entry cap but cumulatively
    explosive.
    """
    total_compressed = 0
    total_uncompressed = 0
    file_count = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        file_count += 1
        if file_count > _MAX_ENTRY_COUNT:
            raise SkillBundleTooLargeError(
                f"bundle has too many files (max {_MAX_ENTRY_COUNT})"
            )
        if info.file_size > _MAX_UNCOMPRESSED_PER_ENTRY:
            raise SkillBundleTooLargeError(
                f"entry too large when uncompressed: {info.filename!r} "
                f"({info.file_size} bytes)"
            )
        total_compressed += info.compress_size
        total_uncompressed += info.file_size
        # Absolute ceiling, checked as we accumulate so a long entry list
        # whose declared sizes sum past the cap is rejected without
        # finishing the scan — and, critically, before ``normalize_bundle``
        # would try to materialize that expansion in memory.
        if total_uncompressed > _MAX_TOTAL_UNCOMPRESSED:
            raise InvalidSkillBundleError(
                f"bundle expands to too many bytes uncompressed "
                f"(> {_MAX_TOTAL_UNCOMPRESSED} bytes; refusing to decompress "
                "a possible zip bomb)"
            )
    if (
        total_compressed > 0
        and total_uncompressed / total_compressed > _MAX_COMPRESSION_RATIO
    ):
        raise InvalidSkillBundleError(
            "bundle compression ratio is suspiciously high "
            "(refusing to decompress a possible zip bomb)"
        )


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
    """Find ``SKILL.md`` at the archive root or inside a single wrapper dir.

    Accepts the GitHub-download pattern where everything sits under one
    top-level directory (``repo-main/SKILL.md``). The worker's staging
    step flattens the wrapper after unzip so both layouts end up with
    ``SKILL.md`` at base_dir root. Rejects archives with multiple
    top-level SKILL.md files (ambiguous — one bundle must be one skill).
    """
    names = zf.namelist()

    if _SKILL_FILENAME in names:
        return _SKILL_FILENAME

    # Wrapper-dir case: the archive must have exactly ONE top-level
    # directory, and that directory must contain SKILL.md. Reject if
    # there are multiple top-level entries (files or dirs) — otherwise
    # the worker's flatten step (``find -type d | head -1``) could
    # pick the wrong directory.
    top_levels = {n.split("/", 1)[0] for n in names if "/" in n}
    top_levels = {t for t in top_levels if t}
    # Also count root-level files (entries without "/") as top-level items
    root_files = {n for n in names if "/" not in n}
    if len(top_levels) == 1 and not root_files:
        wrapper = next(iter(top_levels))
        candidate = f"{wrapper}/{_SKILL_FILENAME}"
        if candidate in names:
            return candidate

    raise SkillMdMissingError(
        f"{_SKILL_FILENAME} not found in bundle. The zip must contain "
        f"SKILL.md either at its root or inside a single top-level directory."
    )


def _decode_skill_md(data: bytes) -> str:
    """Decode SKILL.md bytes, dropping a leading UTF-8 BOM.

    ``utf-8-sig`` is ``utf-8`` plus BOM stripping. Windows editors emit the
    BOM by default; left in place it sits before the opening ``---``, the
    fence no longer matches at position 0, and the whole frontmatter block
    reads as body.
    """
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SkillBundleUnreadableError(f"SKILL.md is not valid UTF-8: {exc}") from exc


def _parse_skill_md_text(raw: str) -> ParsedSkill:
    """Validate decoded SKILL.md text and extract its metadata."""
    meta, body = _split_frontmatter(raw)
    if meta is None:
        raise SkillFrontmatterInvalidError(
            "SKILL.md has no YAML frontmatter: start the file with a line "
            "containing only `---`, then the fields (`name: my-skill`), "
            "then a closing `---`."
        )

    # Per the Agent Skills spec, ``name`` is required in frontmatter.
    name = meta.get("name")
    if not name:
        raise SkillFrontmatterInvalidError(
            "SKILL.md frontmatter must include a 'name' field (e.g. `name: my-skill`)."
        )
    _validate_skill_name(name)
    _reject_oversized_body(body)
    # ``description`` falls back to the first non-empty body line if
    # omitted from frontmatter — stricter enforcement isn't worth the
    # UX cost since many real SKILL.md files rely on body-as-description.
    description = _clip(
        meta.get("description") or _first_nonempty_line(body) or name,
        _MAX_DESCRIPTION_CHARS,
    )
    display_name, display_name_i18n = _extract_display_name(meta, name)
    return ParsedSkill(
        name=name,
        display_name=display_name,
        display_name_i18n=display_name_i18n,
        description=description,
    )


def _split_frontmatter(content: str) -> tuple[dict[str, Any] | None, str]:
    """Parse YAML frontmatter between ``---`` fences.

    Uses ``yaml.safe_load`` to handle multiline scalars (``>-``, ``|``,
    etc.) that real-world SKILL.md files use for long descriptions.

    Returns ``None`` for the fields when there is no fence at all, versus an
    empty dict when a fence is there but holds nothing usable — the caller
    reports a different error for each, so they must not collapse.
    """
    import yaml

    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None, content
    try:
        raw = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        raw = None
    if not isinstance(raw, dict):
        return {}, content[match.end():]
    # Everything is coerced to ``str`` (the common case: name / description,
    # which callers treat as strings) — EXCEPT the Agent Skills spec's
    # ``metadata`` block, kept structured so callers can read author-defined
    # fields like ``metadata.display_name`` instead of a stringified dict.
    # Scoping the exception to ``metadata`` keeps a malformed scalar field
    # (e.g. ``name:`` given an object) a clean validation reject, not a crash.
    meta: dict[str, Any] = {}
    for k, v in raw.items():
        if v is None:
            continue
        key = str(k)
        meta[key] = v if key == "metadata" and isinstance(v, dict) else str(v)
    return meta, content[match.end():]


def _extract_display_name(
    meta: dict[str, Any], name: str
) -> tuple[str, dict[str, str] | None]:
    """Read ``metadata.display_name`` (string or ``{locale: name}`` map) into a
    ``(default, per-locale map)`` pair.

    The Agent Skills spec reserves the ``metadata`` block for author-defined
    fields, so a bundle can ship a localized title while ``name`` stays an
    ASCII slug. Shape handling lives in :mod:`app.services.skill_i18n`; falls
    back to ``name`` when absent or unusable.
    """
    metadata = meta.get("metadata")
    raw = metadata.get("display_name") if isinstance(metadata, dict) else None
    return parse_display_name(raw, name)


def _first_nonempty_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _validate_skill_name(name: str) -> None:
    if len(name) > _MAX_NAME_CHARS or not _SKILL_NAME_RE.fullmatch(name):
        raise InvalidSkillNameError(
            "invalid skill name: use 1-64 lowercase letters, numbers, "
            "and single hyphens; do not start or end with a hyphen"
        )


def _reject_oversized_body(body: str) -> None:
    """Reject a SKILL.md whose body would blow the agent's context budget.

    The body (frontmatter stripped) is injected and kept through compaction
    when the skill is invoked, so an oversized one is a permanent context
    cost. See :data:`_MAX_BODY_BYTES`.
    """
    size = len(body.encode("utf-8"))
    if size > _MAX_BODY_BYTES:
        raise SkillBodyTooLargeError(size=size, limit=_MAX_BODY_BYTES)


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
