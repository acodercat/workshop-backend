"""A session's project, on the way in (``SessionCreate`` / ``SessionUpdate``)."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.session import AgentType
from app.schemas.session import SessionCreate, SessionUpdate


def test_a_session_in_a_project_is_full() -> None:
    # The agent reads the project's files from its sandbox.
    with pytest.raises(ValidationError):
        SessionCreate(project_id=uuid4(), agent_type=AgentType.LITE)
    assert SessionCreate(project_id=uuid4()).agent_type is AgentType.FULL


def test_lite_outside_a_project_is_still_allowed() -> None:
    assert SessionCreate(agent_type=AgentType.LITE).project_id is None


@pytest.mark.parametrize(
    ("body", "moves", "to"),
    [
        ({"title": "renamed"}, False, None),
        ({"project_id": None}, True, None),
        (
            {"project_id": "00000000-0000-0000-0000-000000000001"},
            True,
            "00000000-0000-0000-0000-000000000001",
        ),
    ],
)
def test_a_move_is_told_apart_from_no_move(body: dict, moves: bool, to: str | None) -> None:
    # ``null`` moves the session out; leaving the field out changes nothing.
    update = SessionUpdate(**body)
    assert ("project_id" in update.model_fields_set) is moves
    assert (str(update.project_id) if update.project_id else None) == to
