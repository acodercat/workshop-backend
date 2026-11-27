"""Summary templates: the built-in ones, the user's own, and their default.

Built-ins ship with the code and are read-only; copying one is how a user
starts from it. Every write to a personal template is a compare-and-swap
on its ``version``, so two tabs editing one template cannot silently
overwrite each other.
"""

import json
import uuid
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import LibraryChangedError, MeetingTemplateNotFoundError
from app.models.meeting_library import MeetingLibraryPreference, MeetingTemplate
from app.schemas.meeting_library import (
    SummaryTemplate,
    TemplateDraft,
    TemplateList,
    TemplateOptions,
    TemplateUpdate,
)

# What a user's default is when they have not chosen one.
FALLBACK_DEFAULT = "public-general"


@cache
def _bundled() -> dict[str, Any]:
    """The built-in templates and the prompt that writes to any template,
    shipped together because they were authored together."""
    data: dict[str, Any] = json.loads(
        (Path(__file__).parent / "data" / "summary_templates.json").read_text()
    )
    return data


def builtin_templates() -> tuple[dict[str, Any], ...]:
    return tuple(_bundled()["builtin_templates"])


def writing_prompt() -> str:
    """The system prompt for writing minutes to a template."""
    return str(_bundled()["system_prompt"])


def default_draft() -> TemplateDraft:
    """The template minutes are written to when none is chosen."""
    draft = builtin_draft(FALLBACK_DEFAULT)
    if draft is None:
        raise RuntimeError(f"built-in template {FALLBACK_DEFAULT!r} is missing")
    return draft


def builtin_draft(template_id: str) -> TemplateDraft | None:
    for template in builtin_templates():
        if template["id"] == template_id:
            return TemplateDraft.model_validate(
                {key: template[key] for key in ("title", "description", "sections", "options")}
            )
    return None


def _personal(row: MeetingTemplate, default_id: str) -> SummaryTemplate:
    return SummaryTemplate(
        id=str(row.id),
        scope="personal",
        version=row.version,
        title=row.title,
        description=row.description,
        sections=row.sections,
        options=TemplateOptions.model_validate(row.options),
        can_delete=True,
        is_default=str(row.id) == default_id,
    )


def _public(template: dict[str, Any], default_id: str) -> SummaryTemplate:
    return SummaryTemplate(
        id=template["id"],
        scope="public",
        version=template["version"],
        title=template["title"],
        description=template["description"],
        sections=template["sections"],
        options=TemplateOptions.model_validate(template["options"]),
        can_delete=False,
        is_default=template["id"] == default_id,
    )


async def _rows(db: AsyncSession, user_id: int) -> list[MeetingTemplate]:
    return list(
        (
            await db.execute(
                select(MeetingTemplate)
                .where(MeetingTemplate.user_id == user_id)
                # Most recently edited first: the one being worked on is on top.
                .order_by(MeetingTemplate.updated_at.desc(), MeetingTemplate.id)
            )
        ).scalars()
    )


async def _default_id(db: AsyncSession, user_id: int, rows: list[MeetingTemplate]) -> str:
    """The user's default, if it still exists; else the fallback."""
    chosen = (
        await db.execute(
            select(MeetingLibraryPreference.default_template_id).where(
                MeetingLibraryPreference.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    existing = {str(row.id) for row in rows} | {t["id"] for t in builtin_templates()}
    return chosen if chosen is not None and chosen in existing else FALLBACK_DEFAULT


async def list_templates(db: AsyncSession, user_id: int) -> TemplateList:
    rows = await _rows(db, user_id)
    default_id = await _default_id(db, user_id, rows)
    return TemplateList(
        personal=[_personal(row, default_id) for row in rows],
        public=[_public(template, default_id) for template in builtin_templates()],
    )


async def _owned(
    db: AsyncSession, user_id: int, template_id: str, *, for_update: bool = False
) -> MeetingTemplate:
    try:
        key = uuid.UUID(template_id)
    except ValueError:
        raise MeetingTemplateNotFoundError("No such template.") from None
    query = select(MeetingTemplate).where(
        MeetingTemplate.id == key, MeetingTemplate.user_id == user_id
    )
    if for_update:
        query = query.with_for_update()
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise MeetingTemplateNotFoundError("No such template.")
    return row


def _columns(draft: TemplateDraft) -> dict[str, Any]:
    return {
        "title": draft.title,
        "description": draft.description,
        "sections": list(draft.sections),
        "options": draft.options.model_dump(exclude_none=True),
    }


async def _result(db: AsyncSession, user_id: int, row: MeetingTemplate) -> SummaryTemplate:
    await db.commit()
    return _personal(row, await _default_id(db, user_id, [row]))


async def create(db: AsyncSession, user_id: int, draft: TemplateDraft) -> SummaryTemplate:
    row = MeetingTemplate(user_id=user_id, version=1, **_columns(draft))
    db.add(row)
    await db.flush()
    return await _result(db, user_id, row)


async def update_template(
    db: AsyncSession, user_id: int, template_id: str, body: TemplateUpdate
) -> SummaryTemplate:
    row = await _owned(db, user_id, template_id, for_update=True)
    if row.version != body.expected_version:
        raise LibraryChangedError("The template changed since it was read.")
    for key, value in _columns(body).items():
        setattr(row, key, value)
    row.version += 1
    row.updated_at = datetime.now(UTC)
    return await _result(db, user_id, row)


async def copy(db: AsyncSession, user_id: int, template_id: str) -> SummaryTemplate:
    """Copy a personal or built-in template into a new personal one."""
    source = builtin_draft(template_id)
    if source is None:
        row = await _owned(db, user_id, template_id)
        source = TemplateDraft.model_validate(
            {
                "title": row.title,
                "description": row.description,
                "sections": row.sections,
                "options": row.options,
            }
        )
    # The copy keeps the title: it lands among the user's own templates,
    # where they rename it if they want it told apart.
    return await create(db, user_id, source)


async def remove(db: AsyncSession, user_id: int, template_id: str) -> None:
    row = await _owned(db, user_id, template_id, for_update=True)
    await db.delete(row)
    # Deleting the default leaves the fallback in its place.
    await db.execute(
        update(MeetingLibraryPreference)
        .where(
            MeetingLibraryPreference.user_id == user_id,
            MeetingLibraryPreference.default_template_id == template_id,
        )
        .values(default_template_id=None)
    )
    await db.commit()


async def set_default(db: AsyncSession, user_id: int, template_id: str) -> SummaryTemplate:
    builtin = next((t for t in builtin_templates() if t["id"] == template_id), None)
    if builtin is not None:
        await _save_default(db, user_id, template_id)
        return _public(builtin, template_id)
    personal = await _owned(db, user_id, template_id)
    await _save_default(db, user_id, template_id)
    return _personal(personal, template_id)


async def _save_default(db: AsyncSession, user_id: int, template_id: str) -> None:
    await db.execute(
        insert(MeetingLibraryPreference)
        .values(user_id=user_id, default_template_id=template_id)
        .on_conflict_do_update(
            index_elements=[MeetingLibraryPreference.user_id],
            set_={"default_template_id": template_id},
        )
    )
    await db.commit()
