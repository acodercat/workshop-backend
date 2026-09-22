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
endpoint has to know how the shape works. The shape itself — column plus
``{locale: text}`` map, and the fallback order — lives in
:mod:`app.services.localized_text`, which the Connector catalog uses too; what
stays here is the skill vocabulary spoken by its callers.
"""

from __future__ import annotations

from typing import Any

from app.services.localized_text import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    normalize_locale,
    parse,
    resolve,
)

__all__ = [
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "normalize_locale",
    "parse_display_name",
    "resolve_display_name",
]

# Matches the ``skill.display_name`` column width.
_MAX_DISPLAY_NAME_CHARS = 128


def parse_display_name(value: Any, fallback: str) -> tuple[str, dict[str, str] | None]:
    """Split a frontmatter ``display_name`` into ``(default, per-locale map)``.

    *fallback* is the caller's slug, used when the frontmatter carries nothing
    usable.
    """
    return parse(value, fallback, max_chars=_MAX_DISPLAY_NAME_CHARS)


def resolve_display_name(display_name: str, i18n: dict[str, str] | None, locale: str) -> str:
    """Pick the name for *locale*, falling back to the default language then
    the canonical ``display_name``."""
    return resolve(display_name, i18n, locale)
