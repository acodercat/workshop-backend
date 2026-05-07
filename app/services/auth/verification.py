"""Registration verification code lifecycle.

The contract between :func:`request_register_code` and
:func:`verify_register_code`:

- A 6-digit code is stored in Redis under
  ``email_verify:{email}`` with ``register_code_ttl_seconds`` TTL
  (10 min default).
- The value is JSON ``{code, sent_at, attempts}``. Resending before
  the cooldown window expires raises :class:`CooldownError`;
  resending past the cooldown invalidates the previous code and
  starts fresh.
- :func:`verify_register_code` matches the supplied code, increments
  ``attempts`` on a miss, and consumes (deletes) the entry on a hit.
  After ``register_code_max_attempts`` misses the code is
  invalidated — the user must request a fresh one.

Why Redis instead of a DB table:

- Single-row state per email, ephemeral by definition (TTL).
- Atomic SET-with-TTL + GET keep the resend / verify paths simple.
- Survives backend restarts (DB-level state is overkill for codes).
"""

import json
import logging
import secrets
import time

import redis.asyncio as aioredis

from app.services.email import (
    EmailSendError,
    render_register_verification,
    send_html_email,
)
from app.services.email.templates import Locale
from app.services.redis import email_verify_key
from config import settings

logger = logging.getLogger(__name__)


class VerificationError(Exception):
    """Base for all verification-flow refusals. Carries a short
    machine-readable ``reason`` plus a human-readable message; the
    endpoint maps reason → HTTP status code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _generate_code() -> str:
    """Cryptographically random 6-digit code, zero-padded."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def request_register_code(
    redis: aioredis.Redis, *, email: str, locale: Locale = "zh-CN"
) -> None:
    """Generate + send a verification code for ``email``.

    Refuses with :class:`VerificationError` (``reason="cooldown"``) if
    a previous code was sent within the cooldown window. Refuses with
    ``reason="email_send_failed"`` if Aliyun rejects the send (in
    which case nothing is stored in Redis — the user can retry).
    """
    key = email_verify_key(email)
    now = int(time.time())

    # Cooldown check: read prior state, reject if the previous send
    # was too recent. ``GET`` is racy with concurrent requests but
    # the worst case is two codes sent in the same second to the
    # same address — acceptable; the latest one always wins on
    # verify.
    raw = await redis.get(key)
    if raw is not None:
        prior = json.loads(raw)
        elapsed = now - prior["sent_at"]
        cooldown = settings.register_code_resend_cooldown_seconds
        if elapsed < cooldown:
            remaining = cooldown - elapsed
            raise VerificationError(
                reason="cooldown",
                message=f"请 {remaining} 秒后再试",
            )

    code = _generate_code()
    subject, html_body = render_register_verification(code=code, locale=locale)

    # Send first; only persist on success. Order matters: if Aliyun
    # rejects, we don't want a stale code blocking the user from
    # retrying within the cooldown window.
    try:
        await send_html_email(to=email, subject=subject, html_body=html_body)
    except EmailSendError as exc:
        logger.warning("register code send failed: email=%s reason=%s", email, exc.message)
        raise VerificationError(
            reason="email_send_failed",
            message="验证码发送失败，请稍后重试",
        ) from exc

    payload = {"code": code, "sent_at": now, "attempts": 0}
    await redis.set(
        key,
        json.dumps(payload),
        ex=settings.register_code_ttl_seconds,
    )
    logger.info("register code issued: email=%s", email)


async def verify_register_code(
    redis: aioredis.Redis, *, email: str, code: str
) -> None:
    """Verify and consume the pending code for ``email``.

    Success → deletes the entry (one-shot). Mismatch → increments
    ``attempts``; if it hits the cap the entry is deleted and future
    verifies surface as ``reason="not_found"``. Returns ``None`` on
    success; raises :class:`VerificationError` on every failure path
    so the endpoint can map reasons to HTTP statuses uniformly.
    """
    key = email_verify_key(email)

    raw = await redis.get(key)
    if raw is None:
        raise VerificationError(
            reason="not_found",
            message="验证码已过期或不存在，请重新获取",
        )
    state = json.loads(raw)

    if state["code"] == code:
        await redis.delete(key)
        return

    attempts = state["attempts"] + 1
    if attempts >= settings.register_code_max_attempts:
        await redis.delete(key)
        logger.info(
            "register code locked after max attempts: email=%s",
            email,
        )
        raise VerificationError(
            reason="too_many_attempts",
            message="验证码错误次数过多，请重新获取",
        )

    # Update with new attempt count, preserving the original TTL — we
    # don't want a wrong-guess to extend the code's validity.
    ttl = await redis.ttl(key)
    state["attempts"] = attempts
    await redis.set(key, json.dumps(state), ex=max(ttl, 1))
    raise VerificationError(
        reason="mismatch",
        message="验证码错误",
    )
