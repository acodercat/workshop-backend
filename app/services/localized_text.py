"""One shape for every piece of operator text the UI shows in two languages.

A localizable field is stored as a pair: a plain column holding the canonical
text, and a nullable ``{locale: text}`` JSONB map beside it. The column is what
every non-localized row has and what any locale falls back to, so nothing has
to special-case "no translation" — it reads the column, which is always there.

The alternative shapes were both worse here. A column per locale needs a
migration to add a language. A side table needs a join on every read of a
handful of curated rows, and buys indexing and referential integrity that two
locales of display text do not need.

The skills catalog and the Connector catalog both use it.

**Editing the canonical column invalidates the map.** :func:`resolve` reads the
map's default-locale entry *before* falling back to the column, so a map left
behind shadows the new text for every locale — not only the ones it lists. Any
write path that changes a canonical column must therefore clear its map in the
same breath. It is a rule rather than a helper because the sites that apply it
differ in mechanism: ``api/public/skill.py`` builds a dict for one UPDATE,
``api/admin/skill.py`` assigns on the ORM row, ``api/admin/connector.py``
does that and records the discarded map in its audit payload, and
``api/admin/category.py::_name_fields`` is the one that can *keep* a map —
the category console can show and edit the translations, so a rename that
supplies new ones replaces rather than discards. What they share is the
reason, which is written here so none of them has to re-derive it.
"""

from __future__ import annotations

from typing import Any

# UI locales the app ships, mirrored from the frontend's next-intl config.
# ``DEFAULT_LOCALE`` is the canonical fallback stored in the plain column and
# used whenever a request locale is missing / unsupported / untranslated.
SUPPORTED_LOCALES: tuple[str, ...] = ("zh-CN", "en-US")
DEFAULT_LOCALE = "zh-CN"


def normalize_locale(raw: str | None) -> str:
    """Map a request locale onto a supported one, else the default."""
    return raw if raw in SUPPORTED_LOCALES else DEFAULT_LOCALE


def resolve(default: str, i18n: dict[str, str] | None, locale: str) -> str:
    """Pick the text for *locale*, falling back to the default language and
    then to the canonical column."""
    if i18n:
        return i18n.get(locale) or i18n.get(DEFAULT_LOCALE) or default
    return default


def resolve_optional(default: str | None, i18n: dict[str, str] | None, locale: str) -> str | None:
    """:func:`resolve` for a column that may itself be NULL — a description
    nobody wrote stays absent rather than becoming an empty string."""
    if default is None and not i18n:
        return None
    return resolve(default or "", i18n, locale) or None


def parse(
    value: Any, fallback: str, *, max_chars: int | None = None
) -> tuple[str, dict[str, str] | None]:
    """Split an authored value into ``(canonical, per-locale map)``.

    - ``str`` → ``(that string, None)`` — one text for all locales.
    - ``{locale: text}`` → ``(default-locale text, cleaned map)``.
    - missing / unusable → ``(fallback, None)``.
    """
    if isinstance(value, str) and value.strip():
        return _clip(value, max_chars), None
    if isinstance(value, dict):
        cleaned = clean_map(value, max_chars=max_chars)
        if cleaned:
            return cleaned.get(DEFAULT_LOCALE) or next(iter(cleaned.values())), cleaned
    return fallback, None


def clean_map(
    value: dict[str, str] | None, *, max_chars: int | None = None
) -> dict[str, str] | None:
    """Keep only supported locales with non-empty text; ``None`` when nothing
    is left, so "no translations" is SQL ``NULL`` rather than ``{}``."""
    if not value:
        return None
    cleaned = {
        locale: _clip(text, max_chars)
        for locale, text in value.items()
        if locale in SUPPORTED_LOCALES and isinstance(text, str) and text.strip()
    }
    return cleaned or None


def _clip(text: str, max_chars: int | None) -> str:
    text = text.strip()
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
