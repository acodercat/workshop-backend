"""The meeting library — summary templates and vocabularies — and the
deployment's meeting AI settings.

Under their own prefixes rather than ``/meetings/...``: those paths are
meeting ids, and ``/meetings/templates`` would be read as one.
"""

import asyncio

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.api.uploads import read_capped_body
from app.auth import authenticate_and_get_user
from app.errors import TemplateImportTooLargeError
from app.models.user import User
from app.schemas.meeting_library import (
    CategoryName,
    DefaultTemplate,
    SummaryTemplate,
    TemplateDraft,
    TemplateList,
    TemplatePreview,
    TemplateUpdate,
    Vocabulary,
    VocabularyList,
    VocabularyListEntryUpdate,
    VocabularySave,
)
from app.services.meeting import templates, vocabulary
from app.services.meeting.template_import import MAX_UPLOAD_BYTES, import_template

router = APIRouter()


#
# Templates


@router.get("/meeting-library/templates")
async def list_templates(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> TemplateList:
    return await templates.list_templates(db, user.id)


@router.post("/meeting-library/templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    body: TemplateDraft,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> SummaryTemplate:
    return await templates.create(db, user.id, body)


@router.put("/meeting-library/templates/default")
async def set_default_template(
    body: DefaultTemplate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> SummaryTemplate:
    return await templates.set_default(db, user.id, body.template_id)


@router.post("/meeting-library/templates/imports")
async def preview_template_import(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    user: User = Depends(authenticate_and_get_user),
) -> TemplatePreview:
    """Read a template out of an uploaded document (the raw bytes are the
    body). Nothing is saved: the user reviews the preview and creates the
    template from it."""
    data = await read_capped_body(
        request,
        MAX_UPLOAD_BYTES,
        too_large=TemplateImportTooLargeError(
            f"The document is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        ),
    )
    # Unzipping and parsing a document is CPU work; keep it off the loop.
    return await asyncio.to_thread(import_template, filename, data)


@router.put("/meeting-library/templates/{template_id}")
async def update_template(
    template_id: str,
    body: TemplateUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> SummaryTemplate:
    return await templates.update_template(db, user.id, template_id, body)


@router.post("/meeting-library/templates/{template_id}/copies", status_code=status.HTTP_201_CREATED)
async def copy_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> SummaryTemplate:
    return await templates.copy(db, user.id, template_id)


@router.delete("/meeting-library/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> None:
    await templates.remove(db, user.id, template_id)


#
# Vocabularies. ``scope`` is ``personal`` or a project id.


@router.get("/meeting-library/vocabularies")
async def list_vocabularies(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> VocabularyList:
    return await vocabulary.list_vocabularies(db, user.id)


@router.patch("/meeting-library/vocabularies/{scope}")
async def update_vocabulary_entry(
    scope: str,
    body: VocabularyListEntryUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> VocabularyList:
    return await vocabulary.update_list_entry(
        db,
        user.id,
        scope,
        expected_list_version=body.expected_list_version,
        name=body.name,
        hidden=body.hidden,
    )


@router.get("/meeting-library/vocabularies/{scope}")
async def get_vocabulary(
    scope: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> Vocabulary:
    return await vocabulary.read(db, user.id, scope)


@router.put("/meeting-library/vocabularies/{scope}/terms")
async def save_vocabulary_terms(
    scope: str,
    body: VocabularySave,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> Vocabulary:
    return await vocabulary.save_terms(
        db, user.id, scope, expected_version=body.expected_version, terms=body.terms
    )


@router.post(
    "/meeting-library/vocabularies/{scope}/categories", status_code=status.HTTP_201_CREATED
)
async def create_vocabulary_category(
    scope: str,
    body: CategoryName,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> Vocabulary:
    return await vocabulary.create_category(
        db, user.id, scope, expected_version=body.expected_version, name=body.name
    )


@router.patch("/meeting-library/vocabularies/{scope}/categories/{category_id}")
async def rename_vocabulary_category(
    scope: str,
    category_id: str,
    body: CategoryName,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> Vocabulary:
    return await vocabulary.rename_category(
        db, user.id, scope, category_id, expected_version=body.expected_version, name=body.name
    )


@router.delete("/meeting-library/vocabularies/{scope}/categories/{category_id}")
async def delete_vocabulary_category(
    scope: str,
    category_id: str,
    expected_version: int = Query(ge=0),
    delete_terms: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(authenticate_and_get_user),
) -> Vocabulary:
    return await vocabulary.delete_category(
        db,
        user.id,
        scope,
        category_id,
        expected_version=expected_version,
        delete_terms=delete_terms,
    )
