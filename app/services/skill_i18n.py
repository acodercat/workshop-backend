"""Skill display-name localization.

A SKILL.md's ``metadata.display_name`` may be a plain string (one name for
every locale) or a ``{locale: name}`` map. This module is the single home for
that shape:

- :func:`parse_display_name` splits the frontmatter value at upload time into
  a ``(default, per-locale map)`` pair — the default lands in the
  ``skill.display_name`` column, the map (if any) in ``skill.display_name_i18n``.
- :func:`resolve_display_name` collapses it back to one string per request
  locale at read time.

Keeping the whole i18n concern here means neither the bundle parser nor the API
endpoint has to know how the shape works.
"""

from __future__ import annotations

from typing import Any

# UI locales the app ships, mirrored from the frontend's next-intl config.
# ``DEFAULT_LOCALE`` is the canonical fallback stored in ``display_name`` and
# used whenever a request locale is missing / unsupported / untranslated.
SUPPORTED_LOCALES: tuple[str, ...] = ("zh-CN", "en-US")
DEFAULT_LOCALE = "zh-CN"

# Matches the ``skill.display_name`` column width.
_MAX_DISPLAY_NAME_CHARS = 128


def _clip(value: str) -> str:
    """Trim, and ellipsize past the column budget (same idiom as the
    description clipping in ``skill_bundle``)."""
    value = value.strip()
    if len(value) <= _MAX_DISPLAY_NAME_CHARS:
        return value
    return value[: _MAX_DISPLAY_NAME_CHARS - 1] + "…"


def parse_display_name(
    value: Any, fallback: str
) -> tuple[str, dict[str, str] | None]:
    """Split a frontmatter ``display_name`` into ``(default, per-locale map)``.

    - ``str`` → ``(that string, None)`` — one name for all locales.
    - ``{locale: name}`` → ``(default-locale name, cleaned map)`` — only
      supported locales with non-empty string names survive.
    - missing / unusable → ``(fallback, None)`` — the caller's slug.
    """
    if isinstance(value, str) and value.strip():
        return _clip(value), None
    if isinstance(value, dict):
        cleaned = {
            loc: _clip(name)
            for loc, name in value.items()
            if loc in SUPPORTED_LOCALES
            and isinstance(name, str)
            and name.strip()
        }
        if cleaned:
            default = cleaned.get(DEFAULT_LOCALE) or next(iter(cleaned.values()))
            return default, cleaned
    return fallback, None


def resolve_display_name(
    display_name: str, i18n: dict[str, str] | None, locale: str
) -> str:
    """Pick the name for *locale*, falling back to the default language then
    the canonical ``display_name``."""
    if i18n:
        return i18n.get(locale) or i18n.get(DEFAULT_LOCALE) or display_name
    return display_name


def normalize_locale(raw: str | None) -> str:
    """Map a request locale onto a supported one, else the default."""
    return raw if raw in SUPPORTED_LOCALES else DEFAULT_LOCALE
