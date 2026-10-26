"""Per-user concurrent running-task cap (``queue_user_message``).

The cap is enforced by ``_enforce_concurrent_task_limit`` under the
reservation's user-row lock. A real two-enqueue race needs a DB (integration
tier); these cover the boundary logic with a fake session whose ``scalar``
returns a preset running-count.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import pytest

from app.errors import ConcurrentTaskLimitError
from app.services.session import enqueue
from config import settings


class _CountDb:
    """Fake AsyncSession whose ``scalar`` returns a preset running-count."""

    def __init__(self, count: int) -> None:
        self._count = count

    async def scalar(self, _stmt) -> int:
        return self._count


async def test_under_cap_is_allowed() -> None:
    cap = settings.max_concurrent_running_tasks
    # cap-1 already running → this enqueue is the cap-th → allowed.
    await enqueue._enforce_concurrent_task_limit(_CountDb(cap - 1), user_id=1)


async def test_at_cap_rejects() -> None:
    cap = settings.max_concurrent_running_tasks
    # cap already running → this enqueue would be the (cap+1)-th → rejected.
    with pytest.raises(ConcurrentTaskLimitError):
        await enqueue._enforce_concurrent_task_limit(_CountDb(cap), user_id=1)


def test_the_refusal_names_the_cap_for_the_client() -> None:
    # Its own ``error_type`` with the cap in ``details``: the client can say
    # "at most N tasks at once" instead of a generic rate-limit message, and
    # the message carries no user id.
    exc = ConcurrentTaskLimitError(10)

    assert exc.error_type == "concurrent_task_limit"
    assert exc.status_code == 429
    assert exc.details == {"limit": 10}
