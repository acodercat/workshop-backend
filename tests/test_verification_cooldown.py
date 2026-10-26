"""The resend cooldown is its own key, claimed atomically before the send.

It used to be read off the code's own ``sent_at``: a GET-then-SET that let
racing requests all pass, and a wrong-guess lockout — which deletes the code
— took the cooldown with it, so locking a code out skipped the wait.
"""

import pytest

from app.errors import CooldownError, SmsSendFailedError
from app.services.auth import verification


class _FakeRedis:
    """Just the commands ``_request_code`` uses, over a dict."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def ttl(self, key):
        return 60 if key in self.values else -2

    async def delete(self, key):
        self.values.pop(key, None)


async def _sent(code: str) -> None:
    pass


async def _failed(code: str) -> None:
    raise SmsSendFailedError("Verification SMS send failed")


async def test_a_second_request_inside_the_window_is_refused() -> None:
    redis = _FakeRedis()
    await verification._request_code(redis, key="phone_verify:1", send=_sent)

    with pytest.raises(CooldownError):
        await verification._request_code(redis, key="phone_verify:1", send=_sent)


async def test_a_lockout_does_not_clear_the_cooldown() -> None:
    redis = _FakeRedis()
    await verification._request_code(redis, key="phone_verify:1", send=_sent)
    await redis.delete("phone_verify:1")  # what the verify script does on LOCKED

    with pytest.raises(CooldownError):
        await verification._request_code(redis, key="phone_verify:1", send=_sent)


async def test_a_failed_send_releases_the_cooldown() -> None:
    redis = _FakeRedis()
    with pytest.raises(SmsSendFailedError):
        await verification._request_code(redis, key="phone_verify:1", send=_failed)

    await verification._request_code(redis, key="phone_verify:1", send=_sent)
    assert "phone_verify:1" in redis.values
