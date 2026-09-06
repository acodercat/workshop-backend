"""Every paginated console list sorts deterministically.

OFFSET paging over a sort whose keys can tie is free to order the tied
rows differently on each request, so the same row lands on two pages or
on none — a list that silently does not show everything. The session
list has carried the rule (and the comment explaining it) since it was
written; seven others were paging on a bare ``created_at``, and the
platform skills, published by one script, tie on it by the hundred.

Read off the compiled SQL rather than the source so a filter or a sort
spelled through a helper is still seen.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import ast
import inspect
import textwrap

import pytest

from app.api.admin import (
    connector as admin_connector,
    feedback as admin_feedback,
    invite as admin_invite,
    message_feedback as admin_message_feedback,
    payment as admin_payment,
    session as admin_session,
    skill as admin_skill,
    user as admin_user,
)


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


# Endpoint module, function name, and the mapped class whose primary key
# must end the ORDER BY. Staff (sorted by the unique ``username``), the
# audit log (already ends on its id) and the category / catalog lists
# (not paginated) are deliberately absent.
_PAGINATED_LISTS = [
    (admin_user, "list_users", "User"),
    (admin_user, "list_user_sessions", "Session"),
    (admin_session, "list_sessions", "Session"),
    (admin_skill, "list_skills", "Skill"),
    (admin_connector, "list_connectors", "MCPServer"),
    (admin_feedback, "list_feedback", "UserFeedback"),
    (admin_message_feedback, "list_message_feedback", "MessageFeedback"),
    (admin_invite, "list_invite_codes", "InviteCode"),
    (admin_invite, "list_invite_redemptions", "InviteRedemption"),
    (admin_payment, "list_payments", "PaymentOrder"),
]


@pytest.mark.parametrize(
    ("module", "endpoint", "model"),
    _PAGINATED_LISTS,
    ids=[endpoint for _, endpoint, _ in _PAGINATED_LISTS],
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
