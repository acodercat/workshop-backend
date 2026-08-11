"""Pure-function tests for the skill-bundle validator + parser.

No DB / no OSS — exercises :func:`app.services.skill_bundle.parse_bundle`
(metadata extraction) and :func:`validate_zip_structure` (safety guards
that the endpoint must run before any decompression) over in-memory
zips. Covers each rejection path explicitly so a future relaxation
surfaces as a behavior change in a named test.
"""

import io
import zipfile

import pytest

from app.errors import (
    InvalidSkillBundleError,
    InvalidSkillNameError,
    SkillBodyTooLargeError,
    SkillBundleTooLargeError,
    SkillBundleUnreadableError,
    SkillFrontmatterInvalidError,
    SkillMdMissingError,
)
from app.services.skill_bundle import (
    _MAX_BODY_BYTES,
    parse_bundle,
    validate_zip_structure,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _zip(files: dict[str, bytes | str], *, with_symlink: tuple[str, str] | None = None) -> bytes:
    """Build a zip from a {name: content} mapping. Optionally write one
    entry whose external_attr marks it as a Unix symlink."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
        if with_symlink is not None:
            link_name, target = with_symlink
            info = zipfile.ZipInfo(link_name)
            info.create_system = 3  # Unix
            info.external_attr = (0o120000 | 0o777) << 16  # S_IFLNK
            zf.writestr(info, target)
    return buf.getvalue()


def _skill_md(frontmatter: dict[str, str] | None, body: str) -> str:
    """Render a SKILL.md with optional YAML-ish frontmatter."""
    if frontmatter is None:
        return body
    fm = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
    return f"---\n{fm}\n---\n{body}"


# ── happy paths ─────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_skill_md_at_root_with_full_frontmatter(self):
        data = _zip({
            "SKILL.md": _skill_md(
                {"name": "translate", "description": "Translate text."},
                "# body",
            ),
        })
        parsed = parse_bundle(data)
        assert parsed.name == "translate"
        assert parsed.description == "Translate text."

    def test_skill_md_in_single_wrapper_dir(self):
        """GitHub-download pattern: everything under one top-level dir."""
        data = _zip({
            "my-skill-main/SKILL.md": _skill_md(
                {"name": "wrapped", "description": "Wrapped variant."},
                "body",
            ),
            "my-skill-main/scripts/run.sh": "echo hi",
        })
        parsed = parse_bundle(data)
        assert parsed.name == "wrapped"
        assert parsed.description == "Wrapped variant."

    def test_fallback_description_from_first_body_line(self):
        data = _zip({
            "SKILL.md": _skill_md(
                {"name": "x"},
                "\n  first real line  \nsecond line\n",
            ),
        })
        parsed = parse_bundle(data)
        assert parsed.description == "first real line"

    def test_description_clipped_to_spec_limit(self):
        long = "x" * 1200
        data = _zip({
            "SKILL.md": _skill_md({"name": "n", "description": long}, "body"),
        })
        parsed = parse_bundle(data)
        assert len(parsed.description) <= 1024
        assert parsed.description.endswith("…")


class TestNameValidation:
    @pytest.mark.parametrize(
        "name",
        [
            "PDF-Processing",
            "-pdf",
            "pdf-",
            "pdf--processing",
            "pdf_processing",
            "../pdf",
            "x" * 65,
        ],
    )
    def test_invalid_frontmatter_name_rejected(self, name):
        data = _zip({
            "SKILL.md": _skill_md({"name": name, "description": "d"}, "body"),
        })
        with pytest.raises(InvalidSkillNameError):
            parse_bundle(data)

    def test_wrapper_dir_without_frontmatter_name_rejected(self):
        """Wrapper dir accepted for locating SKILL.md, but name validation
        still applies — a missing frontmatter name is rejected."""
        data = _zip({
            "Bad Skill/SKILL.md": _skill_md(None, "# body"),
        })
        with pytest.raises(SkillFrontmatterInvalidError):
            parse_bundle(data)


# ── rejection: structure ────────────────────────────────────────────────────


class TestStructureRejections:
    def test_not_a_zip(self):
        with pytest.raises(SkillBundleUnreadableError):
            parse_bundle(b"this is not a zip file")

    def test_missing_skill_md(self):
        data = _zip({"README.md": "no skill.md"})
        with pytest.raises(SkillMdMissingError):
            parse_bundle(data)

    def test_multiple_top_level_dirs_rejected(self):
        """Multiple top-level dirs → not a single-wrapper layout, rejected."""
        data = _zip({
            "a/SKILL.md": _skill_md({"name": "a"}, "."),
            "b/SKILL.md": _skill_md({"name": "b"}, "."),
        })
        with pytest.raises(SkillMdMissingError):
            parse_bundle(data)


# ── rejection: safety ──────────────────────────────────────────────────────


class TestSafetyRejections:
    @pytest.mark.parametrize(
        "evil_name",
        [
            "../escape.txt",
            "deep/../../escape.txt",
            "/etc/passwd",
            "back\\slash.txt",
        ],
    )
    def test_unsafe_paths(self, evil_name):
        data = _zip({
            evil_name: "x",
            "SKILL.md": _skill_md({"name": "n"}, "body"),
        })
        with pytest.raises(InvalidSkillBundleError, match="refused"):
            validate_zip_structure(data)

    def test_symlink_entry_rejected(self):
        data = _zip(
            {"SKILL.md": _skill_md({"name": "n"}, "body")},
            with_symlink=("link.txt", "/etc/passwd"),
        )
        with pytest.raises(InvalidSkillBundleError, match="symlink"):
            validate_zip_structure(data)


# ── rejection: zip bombs ────────────────────────────────────────────────────


class TestZipBombRejections:
    """Decompression safety — caps come from the constants in
    ``app.services.skill_bundle``. The tests assert *behavior at the
    threshold* rather than hardcoding the numbers, so a future cap
    tweak only requires updating the constants."""

    def test_per_entry_uncompressed_size_too_large(self):
        # Highly compressible single entry past the per-entry cap.
        from app.services.skill_bundle import _MAX_UNCOMPRESSED_PER_ENTRY
        oversized = b"A" * (_MAX_UNCOMPRESSED_PER_ENTRY + 1)
        data = _zip({
            "SKILL.md": _skill_md({"name": "n"}, "body"),
            "huge.txt": oversized,
        })
        with pytest.raises(SkillBundleTooLargeError):
            validate_zip_structure(data)

    def test_too_many_files_rejected(self):
        # File count past the Agent Skills 500-file cap. Tiny files — the
        # count check fires regardless of their size.
        from app.services.skill_bundle import _MAX_ENTRY_COUNT
        entries = {"SKILL.md": _skill_md({"name": "n"}, "body")}
        entries.update({f"f{i}.txt": "x" for i in range(_MAX_ENTRY_COUNT)})
        data = _zip(entries)
        with pytest.raises(SkillBundleTooLargeError):
            validate_zip_structure(data)

    def test_total_uncompressed_too_large_rejected(self):
        # Many entries, each at-or-under the per-entry cap and the file
        # count well under 500, but whose declared uncompressed sizes sum
        # past the absolute total cap. Highly compressible (zeros via
        # DEFLATE) so it stays tiny on the wire — the total-cap check must
        # fire *during* the scan, before the end-of-loop ratio check.
        from app.services.skill_bundle import (
            _MAX_TOTAL_UNCOMPRESSED,
            _MAX_UNCOMPRESSED_PER_ENTRY,
        )
        entry = b"\x00" * _MAX_UNCOMPRESSED_PER_ENTRY
        # One more entry than the cap allows at max per-entry size.
        count = (_MAX_TOTAL_UNCOMPRESSED // _MAX_UNCOMPRESSED_PER_ENTRY) + 1
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("SKILL.md", _skill_md({"name": "n"}, "body"))
            for i in range(count):
                zf.writestr(f"big{i}.bin", entry)
        with pytest.raises(InvalidSkillBundleError, match="uncompressed"):
            validate_zip_structure(buf.getvalue())

    def test_suspicious_compression_ratio_rejected(self):
        # 1 MiB of zeros compressed via DEFLATE → ~1 KiB, ratio ~1000:1.
        # Stays under both size caps, must fall on the ratio check.
        # Built manually (not via ``_zip`` helper) because the helper
        # defaults to ZIP_STORED — a real zip bomb uses DEFLATE.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("SKILL.md", _skill_md({"name": "n"}, "body"))
            zf.writestr("bomb.bin", b"\x00" * (1 * 1024 * 1024))
        with pytest.raises(InvalidSkillBundleError, match="compression ratio"):
            validate_zip_structure(buf.getvalue())

    def test_normal_compressible_bundle_accepted(self):
        # A realistic SKILL.md with prose body — compresses ~3-5×, well
        # under any cap. Regression guard so the ratio check stays
        # tolerant of normal content.
        body = ("Sales report instructions. " * 200)
        data = _zip({
            "SKILL.md": _skill_md({"name": "ok"}, body),
        })
        validate_zip_structure(data)  # does not raise
        assert parse_bundle(data).name == "ok"


# ── rejection: encoding ────────────────────────────────────────────────────


class TestEncodingRejections:
    def test_skill_md_not_utf8(self):
        # Latin-1 bytes that aren't valid UTF-8
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SKILL.md", b"\xff\xfe\xfdname: x")
        with pytest.raises(SkillBundleUnreadableError):
            parse_bundle(buf.getvalue())


# ── body size cap ───────────────────────────────────────────────────────────


class TestBodySizeCap:
    def test_body_at_limit_accepted(self):
        # Body (frontmatter excluded) exactly at the cap is still allowed.
        body = "x" * _MAX_BODY_BYTES
        data = _zip({"SKILL.md": _skill_md({"name": "big"}, body)})
        assert parse_bundle(data).name == "big"

    def test_oversized_body_rejected(self):
        body = "x" * (_MAX_BODY_BYTES + 1)
        data = _zip({"SKILL.md": _skill_md({"name": "big"}, body)})
        with pytest.raises(SkillBodyTooLargeError):
            parse_bundle(data)

    def test_the_refusal_carries_both_numbers(self):
        # The whole reason this kind exists. "Too large" leaves the uploader
        # guessing how much to move out; the client interpolates these two
        # into its own sentence, so they have to reach it — and they have to
        # agree with the English fallback, which is why one constructor
        # writes both from the same values.
        body = "x" * (_MAX_BODY_BYTES + 500)
        data = _zip({"SKILL.md": _skill_md({"name": "big"}, body)})

        with pytest.raises(SkillBodyTooLargeError) as caught:
            parse_bundle(data)

        assert caught.value.details == {
            "size": len(body.encode("utf-8")),
            "limit": _MAX_BODY_BYTES,
        }
        assert str(caught.value.details["size"]) in caught.value.message
        assert str(_MAX_BODY_BYTES) in caught.value.message

    def test_cap_counts_utf8_bytes_not_chars(self):
        # Fewer characters than the byte cap, but each '✓' is 3 UTF-8 bytes,
        # so the byte count exceeds the cap and must be rejected.
        body = "✓" * ((_MAX_BODY_BYTES // 3) + 1)
        assert len(body) < _MAX_BODY_BYTES
        assert len(body.encode("utf-8")) > _MAX_BODY_BYTES
        data = _zip({"SKILL.md": _skill_md({"name": "uni"}, body)})
        with pytest.raises(SkillBodyTooLargeError):
            parse_bundle(data)

    def test_missing_name_reported_before_oversized_body(self):
        # A huge file with no frontmatter should hear the actionable
        # structural error ("missing name"), not "too large".
        data = _zip({"SKILL.md": "x" * (_MAX_BODY_BYTES + 1)})
        with pytest.raises(SkillFrontmatterInvalidError):
            parse_bundle(data)
