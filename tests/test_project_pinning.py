"""Pinning a project: ``PATCH /projects/{id}`` with ``pinned``, which is not
a change to the project."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.api.public import project as endpoint
from app.models.project import Project
from app.schemas.project import ProjectUpdate

_NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _project(*, pinned: bool) -> Project:
    return Project(
        id=uuid4(),
        user_id=7,
        name="p",
        instructions="",
        icon=None,
        created_at=_NOW,
        updated_at=_NOW,
        pinned_at=_NOW if pinned else None,
    )


async def _patch(monkeypatch, project: Project, body: ProjectUpdate) -> SimpleNamespace:
    """Run the handler; return what it was about to write to the row."""
    written = SimpleNamespace()

    async def refresh(row: Project) -> None:
        # The database resolves the SQL expressions a real refresh reads
        # back; here they are recorded, then replaced.
        written.updated_at, written.pinned_at = row.updated_at, row.pinned_at
        row.updated_at = _NOW
        if row.pinned_at is not None:
            row.pinned_at = _NOW

    db = MagicMock(commit=AsyncMock(), refresh=refresh)
    monkeypatch.setattr(
        endpoint.project_ownership, "require_owned_project", AsyncMock(return_value=project)
    )
    # Past the rate limiter, which wants a live request.
    await endpoint.update_project.__wrapped__(
        request=None,
        project_id=project.id,
        body=body,
        db=db,
        current_user=SimpleNamespace(id=project.user_id),
    )
    return written


async def test_pinning_stamps_the_time(monkeypatch) -> None:
    written = await _patch(monkeypatch, _project(pinned=False), ProjectUpdate(pinned=True))

    assert str(written.pinned_at) == "now()"


async def test_unpinning_clears_it(monkeypatch) -> None:
    written = await _patch(monkeypatch, _project(pinned=True), ProjectUpdate(pinned=False))

    assert written.pinned_at is None


async def test_pinning_a_pinned_project_keeps_its_place(monkeypatch) -> None:
    written = await _patch(monkeypatch, _project(pinned=True), ProjectUpdate(pinned=True))

    assert written.pinned_at == _NOW


@pytest.mark.parametrize("pinned", [True, False])
async def test_only_pinning_keeps_updated_at(monkeypatch, pinned: bool) -> None:
    written = await _patch(monkeypatch, _project(pinned=not pinned), ProjectUpdate(pinned=pinned))

    # Set to itself, so ``onupdate`` does not stamp now(): the project
    # would otherwise jump to the top of the list.
    assert written.updated_at is Project.updated_at


async def test_a_rename_sent_with_a_pin_still_counts_as_a_change(monkeypatch) -> None:
    project = _project(pinned=False)

    written = await _patch(monkeypatch, project, ProjectUpdate(name="renamed", pinned=True))

    assert project.name == "renamed"
    assert str(written.pinned_at) == "now()"
    assert written.updated_at is not Project.updated_at


async def test_a_rename_alone_leaves_the_pin_alone(monkeypatch) -> None:
    written = await _patch(monkeypatch, _project(pinned=True), ProjectUpdate(name="renamed"))

    assert written.pinned_at == _NOW
    assert written.updated_at is not Project.updated_at
