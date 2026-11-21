"""Unit tests for the in-process fan-out hub (``app.services.user.hub``).

Pure in-memory: exercises subscribe / dispatch / unsubscribe / back-
pressure directly, without a Redis connection or the reader task.
"""

import orjson

from app.services.user.hub import _QUEUE_MAXSIZE, UserEventHub


def _payload(user_id: int, **event) -> bytes:
    return orjson.dumps({"user_id": user_id, "event": event})


class TestUserEventHub:
    def test_dispatch_routes_event_to_subscriber(self):
        hub = UserEventHub(redis=None)
        queue = hub.subscribe(7)

        hub._dispatch(_payload(7, type="session.status_changed", status="COMPLETE"))

        assert queue.get_nowait() == {
            "type": "session.status_changed",
            "status": "COMPLETE",
        }

    def test_fans_out_to_every_subscriber_of_a_user(self):
        hub = UserEventHub(redis=None)
        q1, q2 = hub.subscribe(7), hub.subscribe(7)

        hub._dispatch(_payload(7, type="x"))

        assert q1.get_nowait() == {"type": "x"}
        assert q2.get_nowait() == {"type": "x"}

    def test_ignores_users_with_no_local_subscribers(self):
        hub = UserEventHub(redis=None)
        queue = hub.subscribe(7)

        hub._dispatch(_payload(99, type="x"))  # different user → routed nowhere

        assert queue.empty()

    def test_unsubscribe_stops_delivery_and_drops_empty_user(self):
        hub = UserEventHub(redis=None)
        queue = hub.subscribe(7)

        hub.unsubscribe(7, queue)

        assert 7 not in hub._subscribers
        hub._dispatch(_payload(7, type="x"))  # no subscribers → no error
        assert queue.empty()

    def test_full_queue_drops_oldest(self):
        hub = UserEventHub(redis=None)
        queue = hub.subscribe(7)

        for sequence in range(_QUEUE_MAXSIZE + 5):
            hub._dispatch(_payload(7, type="x", n=sequence))

        # Queue caps at maxsize; the 5 oldest events were dropped.
        assert queue.qsize() == _QUEUE_MAXSIZE
        assert queue.get_nowait()["n"] == 5
