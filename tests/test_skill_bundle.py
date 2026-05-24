"""Pure-function tests for the skill-bundle parser.

No DB / no OSS — exercises :func:`app.services.skill_bundle.parse_bundle`
over in-memory zips. Covers each rejection path explicitly so a future
relaxation surfaces as a behavior change in a named test.
"""

import io
import zipfile

import pytest

from app.services.skill_bundle import (
    InvalidSkillBundleError,
    parse_bundle,
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

    def test_skill_md_in_single_top_level_dir(self):
        data = _zip({
            "my-skill-main/SKILL.md": _skill_md(
                {"name": "wrapped", "description": "Wrapped variant."},
                "body",
            ),
            "my-skill-main/scripts/run.sh": "echo hi",
            "my-skill-main/README.md": "ignored",
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

    def test_fallback_name_from_wrapper_dir(self):
        data = _zip({
            "translate/SKILL.md": _skill_md(None, "# body"),
        })
        parsed = parse_bundle(data)
        assert parsed.name == "translate"

    def test_description_clipped_to_column_limit(self):
        long = "x" * 600
        data = _zip({
            "SKILL.md": _skill_md({"name": "n", "description": long}, "body"),
        })
        parsed = parse_bundle(data)
        assert len(parsed.description) <= 512
        assert parsed.description.endswith("…")


# ── rejection: structure ────────────────────────────────────────────────────


class TestStructureRejections:
    def test_not_a_zip(self):
        with pytest.raises(InvalidSkillBundleError, match="not a valid zip"):
            parse_bundle(b"this is not a zip file")

    def test_missing_skill_md(self):
        data = _zip({"README.md": "no skill.md"})
        with pytest.raises(InvalidSkillBundleError, match="SKILL.md not found"):
            parse_bundle(data)

    def test_multiple_top_level_skill_md(self):
        data = _zip({
            "a/SKILL.md": _skill_md({"name": "a"}, "."),
            "b/SKILL.md": _skill_md({"name": "b"}, "."),
        })
        with pytest.raises(InvalidSkillBundleError, match="multiple SKILL.md"):
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
            parse_bundle(data)

    def test_symlink_entry_rejected(self):
        data = _zip(
            {"SKILL.md": _skill_md({"name": "n"}, "body")},
            with_symlink=("link.txt", "/etc/passwd"),
        )
        with pytest.raises(InvalidSkillBundleError, match="symlink"):
            parse_bundle(data)


# ── rejection: encoding ────────────────────────────────────────────────────


class TestEncodingRejections:
    def test_skill_md_not_utf8(self):
        # Latin-1 bytes that aren't valid UTF-8
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SKILL.md", b"\xff\xfe\xfdname: x")
        with pytest.raises(InvalidSkillBundleError, match="UTF-8"):
            parse_bundle(buf.getvalue())
