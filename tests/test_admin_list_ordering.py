"""Every paginated console list sorts deterministically.

OFFSET paging over a sort whose keys can tie is free to order the tied
rows differently on each request, so the same row lands on two pages or
on none — a list that silently does not show everything. The session
list has carried the rule (and the comment explaining it) since it was
written; seven others were paging on a bare ``created_at``, and the
platform skills, published by one script, tie on it by the hundred.

**Which routes have to be here is not a judgement call.** This file used
to carry a hand-written list, and the one paginated route missing from
it — the transactions view, whose ORDER BY is in raw SQL — paged on a
bare timestamp for as long as the list existed. So the table below is
checked against the routes the console actually serves, and a new
paginated endpoint fails that check until someone decides which of the
two rules it lives under.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import ast
import inspect
import textwrap

import pytest
from fastapi import FastAPI

from app.api.admin import (
    connector as admin_connector,
    feedback as admin_feedback,
    invite as admin_invite,
    message_feedback as admin_message_feedback,
    session as admin_session,
    skill as admin_skill,
    user as admin_user,
)
from app.api.admin.router import admin_router
from app.services.credits import transactions


def _last_order_by_key(endpoint) -> str | None:
    """The source of the final positional argument to ``.order_by(...)``.

    Parsed rather than pattern-matched: the clauses run to several lines
    and carry comments, and a regex over that reads the comment as a key.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "order_by"
            and node.args
        ):
            return ast.unparse(node.args[-1])
    return None


# Endpoint module, function name, the mapped class whose primary key must
# end the ORDER BY, and the route it answers. The category / catalog
# lists are absent because they are not paginated.
_PAGINATED_LISTS = [
    (admin_user, "list_users", "User", "/users"),
    (admin_user, "list_user_sessions", "Session", "/users/{user_id}/sessions"),
    (admin_session, "list_sessions", "Session", "/sessions"),
    (admin_skill, "list_skills", "Skill", "/skills"),
    (admin_connector, "list_connectors", "MCPServer", "/connectors"),
    (admin_feedback, "list_feedback", "UserFeedback", "/feedback"),
    (admin_message_feedback, "list_message_feedback", "MessageFeedback", "/message-feedback"),
    (admin_invite, "list_invite_codes", "InviteCode", "/invite-codes"),
    (admin_invite, "list_invite_redemptions", "InviteRedemption", "/invite-redemptions"),
]

#: Paginated routes that satisfy the rule some other way, each checked
#: below. ``/staff`` sorts by ``Staff.username``, which is unique, so its
#: first sort key is already its last. The transactions view is raw SQL
#: over a UNION, where :func:`_last_order_by_key` cannot see anything.
_CHECKED_ANOTHER_WAY = {"/staff", "/users/{user_id}/credits/transactions"}


def _paginated_routes() -> set[str]:
    """Every console route that answers with a page, read off the schema.

    Not off ``admin_router.routes``: those are lazy ``_IncludedRouter``
    objects until an app renders them, and never become ``APIRoute``
    instances this side of a request. The OpenAPI document is where a
    generic ``PaginatedResponse[...]`` becomes visible by name.
    """
    app = FastAPI()
    app.include_router(admin_router)
    return {
        path
        for path, operations in app.openapi()["paths"].items()
        for operation in operations.values()
        if "PaginatedResponse"
        in operation.get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("$ref", "")
    }


def test_every_paginated_route_is_covered_here() -> None:
    """The table above plus the two exceptions must be the whole set —
    otherwise a list can page non-deterministically and this file, whose
    whole subject is that, stays green."""
    assert _paginated_routes() == {path for *_, path in _PAGINATED_LISTS} | _CHECKED_ANOTHER_WAY


def test_the_transaction_view_pages_on_more_than_a_timestamp() -> None:
    """The raw-SQL exception. ``at`` comes from two tables that both
    default ``created_at`` to ``now()`` — the transaction timestamp — so
    rows written together share it exactly, and the sort needs something
    unique after it."""
    sql = " ".join(str(transactions._TRANSACTIONS_PAGE_SQL).split())
    assert "ORDER BY at DESC, tie_source, tie_id DESC" in sql


@pytest.mark.parametrize(
    ("module", "endpoint", "model"),
    [row[:3] for row in _PAGINATED_LISTS],
    ids=[endpoint for _, endpoint, _, _ in _PAGINATED_LISTS],
)
def test_the_sort_ends_on_a_unique_column(module, endpoint, model) -> None:
    """The last sort key must be the primary key: anything before it may
    tie, nothing after it can, so the page boundary is stable between
    two requests."""
    last = _last_order_by_key(getattr(module, endpoint))
    assert last is not None, f"{endpoint} paginates without an explicit sort"
    assert last in (f"{model}.id.desc()", f"{model}.id"), (
        f"{endpoint} ends its sort on {last!r}, which may tie — end on the primary key"
    )
