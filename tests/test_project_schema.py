"""What a project, and a file added to one, may be (``app/schemas/project.py``)."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.project import ProjectCreate, ProjectFileCreate, ProjectUpdate


def test_name_is_trimmed() -> None:
    assert ProjectCreate(name="  Q3 report  ").name == "Q3 report"


@pytest.mark.parametrize("name", ["   ", "​​", "​ ﻿"])
def test_name_with_nothing_visible_is_refused(name: str) -> None:
    # It would list as a blank row in the sidebar.
    with pytest.raises(ValidationError):
        ProjectCreate(name=name)


def test_name_over_the_column_width_is_refused() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(name="x" * 101)


@pytest.mark.parametrize("field", ["name", "instructions"])
def test_null_character_is_refused(field: str) -> None:
    # Not representable in a Postgres text column: an opaque 500 at INSERT.
    with pytest.raises(ValidationError):
        ProjectCreate(**{"name": "ok", field: "a\x00b"})


def test_instructions_default_to_empty_and_may_be_cleared() -> None:
    assert ProjectCreate(name="ok").instructions == ""
    assert ProjectUpdate(instructions="").instructions == ""


def test_instructions_over_the_cap_are_refused() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(name="ok", instructions="x" * 8001)


def test_update_leaves_unsent_fields_alone() -> None:
    body = ProjectUpdate(instructions="Answer in English.")
    assert body.name is None


def test_a_file_comes_from_exactly_one_source() -> None:
    upload = {"file_key": "documents/u/a.md", "filename": "a.md"}
    session_file = {"session_id": uuid4(), "file_key": "outputs/s/t/0_a.md"}
    with pytest.raises(ValidationError):
        ProjectFileCreate()
    with pytest.raises(ValidationError):
        ProjectFileCreate(upload=upload, session_file=session_file)
    assert ProjectFileCreate(upload=upload).session_file is None
