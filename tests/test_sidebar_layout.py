"""``/me/sidebar-layout``: the body's rules, and that a save replaces the
caller's one row."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api.public import sidebar_layout as endpoint
from app.models.sidebar_layout import SidebarLayout
from app.schemas.sidebar_layout import (
    SIDEBAR_ORDER_MAX,
    PinRef,
    SidebarLayoutResponse,
    SidebarLayoutUpdate,
)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


class TestBody:
    def test_projects_and_sessions_sort_together(self) -> None:
        project, session = uuid4(), uuid4()

        body = SidebarLayoutUpdate(
            pin_order=[{"kind": "session", "id": session}, {"kind": "project", "id": project}],
            project_order=[project],
        )

        assert body.pin_order == [
            PinRef(kind="session", id=session),
            PinRef(kind="project", id=project),
        ]

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            SidebarLayoutUpdate(pin_order=[{"kind": "skill", "id": uuid4()}], project_order=[])

    @pytest.mark.parametrize("field", ["pin_order", "project_order"])
    def test_an_item_listed_twice_is_refused(self, field: str) -> None:
        item = {"kind": "project", "id": uuid4()} if field == "pin_order" else uuid4()
        body = {"pin_order": [], "project_order": [], field: [item, item]}

        with pytest.raises(ValidationError, match="at most once"):
            SidebarLayoutUpdate(**body)

    def test_a_runaway_list_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            SidebarLayoutUpdate(
                pin_order=[], project_order=[uuid4() for _ in range(SIDEBAR_ORDER_MAX + 1)]
            )


async def test_a_user_who_never_saved_gets_empty_orders() -> None:
    db = MagicMock(get=AsyncMock(return_value=None))

    layout = await endpoint.get_sidebar_layout(db=db, current_user=SimpleNamespace(id=7))

    assert layout == SidebarLayoutResponse(pin_order=[], project_order=[])
    db.get.assert_awaited_once_with(SidebarLayout, 7)


async def test_a_save_replaces_the_callers_row() -> None:
    project = uuid4()
    body = SidebarLayoutUpdate(
        pin_order=[{"kind": "project", "id": project}], project_order=[project]
    )
    db = MagicMock(execute=AsyncMock(), commit=AsyncMock())

    # Past the rate limiter, which wants a live request.
    saved = await endpoint.replace_sidebar_layout.__wrapped__(
        request=None, body=body, db=db, current_user=SimpleNamespace(id=7)
    )

    statement = db.execute.await_args.args[0]
    sql = _sql(statement)
    assert "INSERT INTO sidebar_layout" in sql
    assert "ON CONFLICT (user_id) DO UPDATE" in sql
    params = statement.compile(dialect=postgresql.dialect()).params
    assert params["user_id"] == 7
    assert params["pin_order"] == [{"kind": "project", "id": str(project)}]
    assert params["project_order"] == [str(project)]
    db.commit.assert_awaited_once()
    assert saved == SidebarLayoutResponse(pin_order=body.pin_order, project_order=[project])
