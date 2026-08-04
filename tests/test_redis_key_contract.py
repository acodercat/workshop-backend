"""The backend's half of the Redis key contract, pinned to literals.

``app/services/redis.py`` and the worker's ``agent_worker/wire/keys.py``
declare the same channel names independently — separate services, separate
venvs, no shared module to import — so a rename on one side drifts until the
two stop talking.

Only the worker used to check, by reading *this* repository's source off disk,
located by counting directories upward from its own tests. That worked while
the two checkouts sat side by side and skipped in silence when they did not,
which included every environment where the worker ships alone.

Each side pins its own half now. The mirror is
``agent-worker/tests/wire/test_keys_contract.py``, carrying the same values.
Neither can prove the other agrees; a rename now fails the suite of whoever
made it, naming where the other half lives.
"""

import pytest

from app.services.redis import (
    CANCEL_CHANNEL_PREFIX,
    SESSION_STREAM_PREFIX,
    SETTLE_NOTIFY_CHANNEL,
    USER_EVENTS_CHANNEL,
    WORKER_NOTIFY_CHANNEL,
)

#: The agreement, as it appears in Redis: (value, this service's constant, what
#: it is for). The constant *names* differ across the two services; only the
#: values are the contract. Mirrored in the worker file named above.
CONTRACT = [
    ("notify:worker", WORKER_NOTIFY_CHANNEL, "worker wake channel"),
    ("settle:notify", SETTLE_NOTIFY_CHANNEL, "credit settle-notify channel"),
    ("session_stream", SESSION_STREAM_PREFIX, "per-session output stream prefix"),
    ("user_events:bus", USER_EVENTS_CHANNEL, "user notification channel"),
    ("cancel", CANCEL_CHANNEL_PREFIX, "cancel signal channel prefix"),
]


@pytest.mark.parametrize("agreed, actual, label", CONTRACT)
def test_channel_name_is_the_agreed_literal(agreed, actual, label):
    assert actual == agreed, (
        f"{label}: this backend now uses {actual!r} where the contract says "
        f"{agreed!r}. The worker declares the same channel in "
        f"agent_worker/wire/keys.py and will go on using the old name — "
        f"change it there in the same breath, and its mirror test with it."
    )
