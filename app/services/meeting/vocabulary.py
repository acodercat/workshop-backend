"""Vocabularies: the terms speech recognition should get right or correct.

A user has a personal vocabulary and one per project they own. Each is a
versioned whole — the terms and their categories move together, a save
replaces the full term list, and every change is a compare-and-swap on the
version the editor read. The row is created on first use, so an
untouched vocabulary reads as empty at version 0.

The list of vocabularies (personal + projects) has a revision of its own,
for renaming or hiding entries in it.
"""

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import (
    LibraryChangedError,
    VocabularyCategoryNotFoundError,
    VocabularyInvalidError,
)
from app.models.meeting_library import (
    MeetingLibraryPreference,
    MeetingVocabulary,
    MeetingVocabularyTerm,
    TermKind,
)
from app.models.project import Project
from app.schemas.meeting_library import (
    Vocabulary,
    VocabularyCategory,
    VocabularyList,
    VocabularyListEntry,
    VocabularyTerm,
)
from app.services.project.ownership import require_owned_project

PERSONAL = "personal"
MAX_CATEGORIES = 100

_KINDS = {"hotword": TermKind.HOTWORD, "replacement": TermKind.REPLACEMENT}
_KIND_NAMES = {value: key for key, value in _KINDS.items()}


async def _project_id(db: AsyncSession, user_id: int, scope: str) -> uuid.UUID | None:
    """The project a scope names, after checking the caller owns it."""
    if scope == PERSONAL:
        return None
    try:
        project_id = uuid.UUID(scope)
    except ValueError:
        raise VocabularyInvalidError("Unknown vocabulary.") from None
    await require_owned_project(db, user_id=user_id, project_id=project_id)
    return project_id


def _vocabulary_query(user_id: int, project_id: uuid.UUID | None):
    query = select(MeetingVocabulary).where(MeetingVocabulary.user_id == user_id)
    if project_id is None:
        return query.where(MeetingVocabulary.project_id.is_(None))
    return query.where(MeetingVocabulary.project_id == project_id)


async def _locked(
    db: AsyncSession, user_id: int, project_id: uuid.UUID | None
) -> MeetingVocabulary:
    """The vocabulary row, created if missing, locked for the write."""
    await db.execute(
        insert(MeetingVocabulary)
        .values(id=uuid.uuid4(), user_id=user_id, project_id=project_id)
        .on_conflict_do_nothing()
    )
    return (await db.execute(_vocabulary_query(user_id, project_id).with_for_update())).scalar_one()


async def _terms(db: AsyncSession, vocabulary_id: uuid.UUID) -> list[MeetingVocabularyTerm]:
    return list(
        (
            await db.execute(
                select(MeetingVocabularyTerm)
                .where(MeetingVocabularyTerm.vocabulary_id == vocabulary_id)
                .order_by(MeetingVocabularyTerm.position)
            )
        ).scalars()
    )


def _term(row: MeetingVocabularyTerm) -> VocabularyTerm:
    return VocabularyTerm.model_validate(
        {
            "id": row.id,
            "version": row.version,
            "kind": _KIND_NAMES[row.kind],
            "text": row.text,
            "replacement": row.replacement,
            "aliases": row.aliases,
            "category": row.category,
            "enabled": row.enabled,
            "evidence": row.evidence,
            "origin": row.origin,
        }
    )


async def _view(db: AsyncSession, scope: str, row: MeetingVocabulary | None) -> Vocabulary:
    if row is None:
        return Vocabulary(scope=scope, version=0, terms=[], categories=[])
    return Vocabulary(
        scope=scope,
        version=row.version,
        terms=[_term(term) for term in await _terms(db, row.id)],
        categories=[VocabularyCategory.model_validate(c) for c in row.categories],
    )


async def read(db: AsyncSession, user_id: int, scope: str) -> Vocabulary:
    project_id = await _project_id(db, user_id, scope)
    row = (await db.execute(_vocabulary_query(user_id, project_id))).scalar_one_or_none()
    return await _view(db, scope, row)


async def _begin(
    db: AsyncSession, user_id: int, scope: str, expected_version: int
) -> MeetingVocabulary:
    project_id = await _project_id(db, user_id, scope)
    row = await _locked(db, user_id, project_id)
    if row.version != expected_version:
        raise LibraryChangedError("The vocabulary changed since it was read.")
    return row


async def _finish(db: AsyncSession, scope: str, row: MeetingVocabulary) -> Vocabulary:
    row.version += 1
    await db.commit()
    return await _view(db, scope, row)


async def save_terms(
    db: AsyncSession,
    user_id: int,
    scope: str,
    *,
    expected_version: int,
    terms: list[VocabularyTerm],
) -> Vocabulary:
    """Replace the vocabulary's terms with ``terms``, in that order.

    A term is identified by kind and spelling: one already in the
    vocabulary keeps its id and gets its version bumped; a term left out is
    removed. The same spelling twice is merged if both say the same thing
    and refused if they do not.
    """
    row = await _begin(db, user_id, scope, expected_version)
    unique = merge_terms(terms, categories={c["id"] for c in row.categories})

    existing = {(_KIND_NAMES[t.kind], t.text): t for t in await _terms(db, row.id)}
    await db.execute(
        delete(MeetingVocabularyTerm).where(MeetingVocabularyTerm.vocabulary_id == row.id)
    )
    await db.flush()
    for position, ((kind, text), term) in enumerate(unique.items()):
        previous = existing.get((kind, text))
        db.add(
            MeetingVocabularyTerm(
                id=previous.id if previous else uuid.uuid4(),
                vocabulary_id=row.id,
                position=position,
                kind=_KINDS[kind],
                text=text,
                replacement=term.replacement,
                aliases=list(term.aliases),
                category=term.category,
                enabled=term.enabled,
                version=(previous.version if previous else 0) + 1,
                evidence=term.evidence.model_dump() if term.evidence else None,
                origin=term.origin.model_dump(exclude_none=True) if term.origin else None,
            )
        )
    return await _finish(db, scope, row)


def merge_terms(
    terms: list[VocabularyTerm], *, categories: set[str]
) -> dict[tuple[str, str], VocabularyTerm]:
    """``terms`` keyed by kind and spelling, first occurrence first.

    A spelling given twice is merged when both say the same thing and
    refused when they do not; a term in a category that no longer exists is
    refused, because the editor that sent it was looking at a stale list.
    """
    unique: dict[tuple[str, str], VocabularyTerm] = {}
    for term in terms:
        key = (term.kind, term.text)
        seen = unique.get(key)
        if seen is not None:
            if seen.replacement != term.replacement:
                raise VocabularyInvalidError("One spelling cannot be corrected two ways.")
            continue
        if term.category is not None and term.category not in categories:
            raise LibraryChangedError("A category was removed since the vocabulary was read.")
        unique[key] = term
    return unique


def _clashes(row: MeetingVocabulary, name: str, *, except_id: str | None = None) -> bool:
    folded = name.casefold()
    return any(c["name"].casefold() == folded and c["id"] != except_id for c in row.categories)


async def create_category(
    db: AsyncSession, user_id: int, scope: str, *, expected_version: int, name: str
) -> Vocabulary:
    row = await _begin(db, user_id, scope, expected_version)
    if len(row.categories) >= MAX_CATEGORIES:
        raise VocabularyInvalidError(f"At most {MAX_CATEGORIES} categories.")
    if _clashes(row, name):
        raise VocabularyInvalidError("A category with that name exists.")
    row.categories = [*row.categories, {"id": str(uuid.uuid4()), "name": name}]
    return await _finish(db, scope, row)


async def rename_category(
    db: AsyncSession,
    user_id: int,
    scope: str,
    category_id: str,
    *,
    expected_version: int,
    name: str,
) -> Vocabulary:
    row = await _begin(db, user_id, scope, expected_version)
    if not any(c["id"] == category_id for c in row.categories):
        raise VocabularyCategoryNotFoundError("No such category.")
    if _clashes(row, name, except_id=category_id):
        raise VocabularyInvalidError("A category with that name exists.")
    row.categories = [{**c, "name": name} if c["id"] == category_id else c for c in row.categories]
    return await _finish(db, scope, row)


async def delete_category(
    db: AsyncSession,
    user_id: int,
    scope: str,
    category_id: str,
    *,
    expected_version: int,
    delete_terms: bool,
) -> Vocabulary:
    """Remove a category. Its terms go with it, or stay uncategorized."""
    row = await _begin(db, user_id, scope, expected_version)
    if not any(c["id"] == category_id for c in row.categories):
        raise VocabularyCategoryNotFoundError("No such category.")
    row.categories = [c for c in row.categories if c["id"] != category_id]
    in_category = (
        MeetingVocabularyTerm.vocabulary_id == row.id,
        MeetingVocabularyTerm.category == category_id,
    )
    if delete_terms:
        await db.execute(delete(MeetingVocabularyTerm).where(*in_category))
    else:
        await db.execute(
            update(MeetingVocabularyTerm)
            .where(*in_category)
            .values(category=None, version=MeetingVocabularyTerm.version + 1)
        )
    return await _finish(db, scope, row)


#
# The list of vocabularies


async def _list_version(db: AsyncSession, user_id: int) -> int:
    """The list's revision; 0 until it is first changed."""
    version = (
        await db.execute(
            select(MeetingLibraryPreference.vocabulary_list_version).where(
                MeetingLibraryPreference.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return version or 0


async def _lock_list_version(db: AsyncSession, user_id: int) -> int:
    """The list's revision, locked for a change to the list."""
    await db.execute(
        insert(MeetingLibraryPreference).values(user_id=user_id).on_conflict_do_nothing()
    )
    return (
        await db.execute(
            select(MeetingLibraryPreference.vocabulary_list_version)
            .where(MeetingLibraryPreference.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one()


async def _entries(db: AsyncSession, user_id: int) -> list[VocabularyListEntry]:
    """The personal vocabulary, then one per live project, newest first."""
    projects = (
        await db.execute(
            select(Project.id, Project.name)
            .where(Project.user_id == user_id, Project.deleted_at.is_(None))
            .order_by(Project.updated_at.desc())
        )
    ).all()
    overrides = {
        row.project_id: row
        for row in (
            await db.execute(select(MeetingVocabulary).where(MeetingVocabulary.user_id == user_id))
        ).scalars()
    }
    entries = []
    for project_id, original in [(None, None), *projects]:
        override = overrides.get(project_id)
        entries.append(
            VocabularyListEntry(
                scope=PERSONAL if project_id is None else str(project_id),
                name=(override.display_name if override and override.display_name else original),
                original_name=original,
                hidden=bool(override and override.hidden),
            )
        )
    return entries


async def list_vocabularies(db: AsyncSession, user_id: int) -> VocabularyList:
    return VocabularyList(
        entries=await _entries(db, user_id), version=await _list_version(db, user_id)
    )


async def update_list_entry(
    db: AsyncSession,
    user_id: int,
    scope: str,
    *,
    expected_list_version: int,
    name: str | None,
    hidden: bool | None,
) -> VocabularyList:
    """Rename a vocabulary in the list, or hide / show it."""
    if await _lock_list_version(db, user_id) != expected_list_version:
        raise LibraryChangedError("The vocabulary list changed since it was read.")
    project_id = await _project_id(db, user_id, scope)
    if name is not None:
        folded = name.casefold()
        if any(
            entry.scope != scope
            and not entry.hidden
            and entry.name is not None
            and entry.name.casefold() == folded
            for entry in await _entries(db, user_id)
        ):
            raise VocabularyInvalidError("A vocabulary with that name exists.")
    row = await _locked(db, user_id, project_id)
    if name is not None:
        row.display_name = name
    if hidden is not None:
        row.hidden = hidden
    await db.execute(
        update(MeetingLibraryPreference)
        .where(MeetingLibraryPreference.user_id == user_id)
        .values(vocabulary_list_version=MeetingLibraryPreference.vocabulary_list_version + 1)
    )
    await db.commit()
    return await list_vocabularies(db, user_id)
