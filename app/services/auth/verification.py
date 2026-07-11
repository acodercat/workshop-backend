"""Verification-code lifecycle (email + phone).

A 6-digit code, ``{code, sent_at, attempts}`` JSON in Redis with a TTL.
Three flows share the same envelope (TTL / cooldown / max-attempts from
the ``register_code_*`` settings) but namespace their state under
different key prefixes so they can't collide:

- **Registration / login** — ``email_verify:{email}`` (channel email,
  delivered by Aliyun DirectMail) or ``phone_verify:{phone}`` (channel
  phone, delivered by Aliyun SMS). See :func:`request_verification_code`
  / :func:`verify_verification_code`.
- **Email change** — ``email_change:{email}`` (verify the *new* address
  an authenticated user wants to switch to). See
  :func:`send_email_change_code` / :func:`verify_email_change_code`.

The state machine (generate, cooldown, attempts, TTL) is delivery-
agnostic; only the **send step** differs by channel, so it's passed in
as a ``send`` callable. A send that fails raises a typed
``*SendFailedError`` and the code is **not** persisted, so the user can
retry immediately.
"""

import json
import logging
import secrets
import time
from typing import Awaitable, Callable, Literal

import redis.asyncio as aioredis

from app.errors import (
    CooldownError,
    EmailSendFailedError,
    InvalidVerificationCodeError,
    SmsSendFailedError,
    TooManyVerificationAttemptsError,
)
from app.services.email import (
    EmailSendError,
    render_email_change_verification,
    render_register_verification,
    send_html_email,
)
from app.services.email.templates import Locale
from app.services.redis import (
    email_change_key,
    email_verify_key,
    phone_change_key,
    phone_verify_key,
)
from app.services.sms import SmsSendError, send_verification_sms
from config import settings

logger = logging.getLogger(__name__)

# The two delivery channels for a registration/login code.
Channel = Literal["email", "phone"]

# Sends the 6-digit code to its destination; raises a ``*SendFailedError``
# on failure (the code is not persisted, so the user can retry).
_SendFn = Callable[[str], Awaitable[None]]

# Atomic compare-and-(consume|increment) for a verification code, run
# server-side so N concurrent wrong guesses can't each read ``attempts``
# before any writes it back — the read-modify-write that made the
# per-code attempt cap defeatable when done in Python. One round trip;
# ``KEEPTTL`` preserves the original expiry so a wrong guess never
# extends validity. Returns one of the ``_VERIFY_*`` sentinels.
#
#   KEYS[1] = state key   ARGV[1] = submitted code
#   ARGV[2] = max_attempts (int)   ARGV[3] = "1" consume / "0" peek
_VERIFY_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 'EXPIRED' end
local state = cjson.decode(raw)
if state.code == ARGV[1] then
    if ARGV[3] == '1' then redis.call('DEL', KEYS[1]) end
    return 'OK'
end
local attempts = state.attempts + 1
if attempts >= tonumber(ARGV[2]) then
    redis.call('DEL', KEYS[1])
    return 'LOCKED'
end
state.attempts = attempts
redis.call('SET', KEYS[1], cjson.encode(state), 'KEEPTTL')
return 'BAD'
"""
_VERIFY_EXPIRED = "EXPIRED"
_VERIFY_OK = "OK"
_VERIFY_LOCKED = "LOCKED"
_VERIFY_BAD = "BAD"


def _generate_code() -> str:
    """Cryptographically random 6-digit code, zero-padded."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _email_sender(identifier: str, render, locale: Locale) -> _SendFn:
    """Build a send callable that renders + emails the code."""

    async def send(code: str) -> None:
        subject, html_body = render(code=code, locale=locale)
        try:
            await send_html_email(to=identifier, subject=subject, html_body=html_body)
        except EmailSendError as exc:
            raise EmailSendFailedError("Verification email send failed") from exc

    return send


def _sms_sender(identifier: str) -> _SendFn:
    """Build a send callable that texts the code via Aliyun SMS."""

    async def send(code: str) -> None:
        try:
            await send_verification_sms(phone=identifier, code=code)
        except SmsSendError as exc:
            raise SmsSendFailedError("Verification SMS send failed") from exc

    return send


async def _request_code(
    redis: aioredis.Redis, *, key: str, send: _SendFn
) -> None:
    """Generic "cooldown-check → generate → send → persist" pipeline.

    Send-then-persist order matters: if the provider rejects, no stale
    code blocks the user from retrying within the cooldown window.
    """
    now = int(time.time())

    # Cooldown check: read prior state, reject if the previous send was
    # too recent. ``GET`` is racy with concurrent requests but the worst
    # case is two codes sent in the same second to the same destination —
    # acceptable; the latest one always wins on verify.
    raw = await redis.get(key)
    if raw is not None:
        prior = json.loads(raw)
        elapsed = now - prior["sent_at"]
        cooldown = settings.register_code_resend_cooldown_seconds
        if elapsed < cooldown:
            raise CooldownError(
                f"Cooldown active; retry in {cooldown - elapsed} seconds"
            )

    code = _generate_code()
    await send(code)  # raises *SendFailedError on failure; persist only after

    payload = {"code": code, "sent_at": now, "attempts": 0}
    await redis.set(key, json.dumps(payload), ex=settings.register_code_ttl_seconds)


async def _verify_code(
    redis: aioredis.Redis,
    *,
    key: str,
    identifier: str,
    code: str,
    log_label: str,
    consume: bool = True,
) -> None:
    """Generic verify-and-(optionally-)consume.

    On a match: deletes the entry when ``consume=True`` (one-shot,
    default), or leaves it untouched when ``consume=False`` (peek mode —
    caller intends to consume in a subsequent call). Mismatch increments
    ``attempts``; on exceeding the cap, deletes the entry and raises
    ``too_many_attempts`` (peek mode never weakens the brute-force
    defense). ``identifier`` is taken only so logs carry the
    human-readable destination.

    The compare-and-(consume|increment) runs atomically via
    :data:`_VERIFY_LUA`, so concurrent wrong guesses can't each read a
    stale ``attempts`` and slip past the cap.
    """
    result = await redis.eval(
        _VERIFY_LUA,
        1,
        key,
        code,
        str(settings.register_code_max_attempts),
        "1" if consume else "0",
    )
    if isinstance(result, bytes):
        result = result.decode()

    if result == _VERIFY_OK:
        return
    if result == _VERIFY_EXPIRED:
        raise InvalidVerificationCodeError(
            "Verification code expired or not found"
        )
    if result == _VERIFY_LOCKED:
        logger.info("%s code locked after max attempts: %s", log_label, identifier)
        raise TooManyVerificationAttemptsError(
            "Too many incorrect verification attempts"
        )
    raise InvalidVerificationCodeError("Incorrect verification code")


def _verify_key(channel: Channel, identifier: str) -> str:
    return (
        phone_verify_key(identifier)
        if channel == "phone"
        else email_verify_key(identifier)
    )


async def request_verification_code(
    redis: aioredis.Redis,
    *,
    channel: Channel,
    identifier: str,
    locale: Locale = "zh-CN",
) -> None:
    """Generate + send a registration/login verification code.

    ``channel="phone"`` texts via Aliyun SMS; ``channel="email"`` emails
    via DirectMail (``locale`` picks the template). Raises
    :class:`~app.errors.CooldownError` within the resend window, or
    :class:`~app.errors.SmsSendFailedError` /
    :class:`~app.errors.EmailSendFailedError` if the provider rejects.
    """
    send = (
        _sms_sender(identifier)
        if channel == "phone"
        else _email_sender(identifier, render_register_verification, locale)
    )
    await _request_code(redis, key=_verify_key(channel, identifier), send=send)
    logger.info("register/%s code issued: %s", channel, identifier)


async def verify_verification_code(
    redis: aioredis.Redis,
    *,
    channel: Channel,
    identifier: str,
    code: str,
    consume: bool = True,
) -> None:
    """Verify the pending registration/login code for ``identifier``.

    ``consume=False`` is peek mode (used by the unified login flow to
    check validity before deciding login vs. register handoff). See
    :func:`_verify_code` for the full failure-mode semantics.
    """
    await _verify_code(
        redis,
        key=_verify_key(channel, identifier),
        identifier=identifier,
        code=code,
        log_label=f"register/{channel}",
        consume=consume,
    )


async def send_email_change_code(
    redis: aioredis.Redis, *, user_id: int, email: str, locale: Locale = "zh-CN"
) -> None:
    """Generate + send an email-change verification code to ``email``
    (the *new* address ``user_id`` wants to switch to). The code is
    bound to the requesting user (see :func:`email_change_key`). Same
    failure modes as :func:`request_verification_code`'s email channel."""
    await _request_code(
        redis,
        key=email_change_key(user_id, email),
        send=_email_sender(email, render_email_change_verification, locale),
    )
    logger.info("email_change code issued: user_id=%s email=%s", user_id, email)


async def verify_email_change_code(
    redis: aioredis.Redis, *, user_id: int, email: str, code: str
) -> None:
    """Verify and consume the pending email-change code for ``user_id``'s
    switch to ``email``. See :func:`_verify_code` for failure modes."""
    await _verify_code(
        redis,
        key=email_change_key(user_id, email),
        identifier=email,
        code=code,
        log_label="email_change",
    )


async def send_phone_change_code(
    redis: aioredis.Redis, *, user_id: int, phone: str
) -> None:
    """Generate + send a phone-change verification code (via Aliyun SMS)
    to ``phone`` (the *new* number ``user_id`` wants to switch to). The
    code is bound to the requesting user (see :func:`phone_change_key`).
    The SMS-channel sibling of :func:`send_email_change_code`; same
    failure modes as :func:`request_verification_code`'s phone channel."""
    await _request_code(
        redis, key=phone_change_key(user_id, phone), send=_sms_sender(phone)
    )
    logger.info("phone_change code issued: user_id=%s phone=%s", user_id, phone)


async def verify_phone_change_code(
    redis: aioredis.Redis, *, user_id: int, phone: str, code: str
) -> None:
    """Verify and consume the pending phone-change code for ``user_id``'s
    switch to ``phone``. See :func:`_verify_code` for failure modes."""
    await _verify_code(
        redis,
        key=phone_change_key(user_id, phone),
        identifier=phone,
        code=code,
        log_label="phone_change",
    )
