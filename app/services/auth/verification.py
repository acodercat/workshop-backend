"""Email verification code lifecycle.

Two flows share this module — they have the same wire shape (6-digit
code, ``{code, sent_at, attempts}`` JSON in Redis with a TTL) but
namespace their state under different prefixes so they can't collide:

- **Registration** (``email_verify:{email}``) — verifying a brand-new
  email address belongs to the person creating the account.
  See :func:`request_register_code` / :func:`verify_register_code`.
- **Email change** (``email_change:{email}``) — verifying that an
  already-authenticated user controls the *new* address they want to
  switch to. See :func:`request_email_change_code` /
  :func:`verify_email_change_code`.

The two prefixes prevent cross-flow contamination: a pending
registration on ``alice@example.com`` doesn't satisfy an email-change
target of the same address (and vice versa). The TTL / cooldown /
max-attempts come from the same ``register_code_*`` settings since
the security envelope is identical.

Why Redis instead of a DB table:

- Single-row state per email, ephemeral by definition (TTL).
- Atomic SET-with-TTL + GET keep the resend / verify paths simple.
- Survives backend restarts (DB-level state is overkill for codes).
"""

import json
import logging
import secrets
import time
from typing import Callable

import redis.asyncio as aioredis

from app.errors import (
    CooldownError,
    EmailSendFailedError,
    InvalidVerificationCodeError,
    TooManyVerificationAttemptsError,
)
from app.services.email import (
    EmailSendError,
    render_email_change_verification,
    render_register_verification,
    send_html_email,
)
from app.services.email.templates import Locale
from app.services.redis import email_change_key, email_verify_key
from config import settings

logger = logging.getLogger(__name__)


_RenderFn = Callable[..., tuple[str, str]]


def _generate_code() -> str:
    """Cryptographically random 6-digit code, zero-padded."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def _request_code(
    redis: aioredis.Redis,
    *,
    key: str,
    email: str,
    locale: Locale,
    render: _RenderFn,
    log_label: str,
) -> None:
    """Generic "generate + send + persist" pipeline used by both the
    register and email-change flows.

    ``render`` produces the locale-aware ``(subject, html_body)``
    pair; ``log_label`` differentiates log messages between flows.
    Send-then-persist order matters: if Aliyun rejects, we don't want
    a stale code blocking the user from retrying within the cooldown
    window.
    """
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
            raise CooldownError(
                f"Cooldown active; retry in {remaining} seconds"
            )

    code = _generate_code()
    subject, html_body = render(code=code, locale=locale)

    try:
        await send_html_email(to=email, subject=subject, html_body=html_body)
    except EmailSendError as exc:
        logger.warning(
            "%s code send failed: email=%s reason=%s",
            log_label, email, exc.message,
        )
        raise EmailSendFailedError(
            "Verification email send failed"
        ) from exc

    payload = {"code": code, "sent_at": now, "attempts": 0}
    await redis.set(
        key,
        json.dumps(payload),
        ex=settings.register_code_ttl_seconds,
    )
    logger.info("%s code issued: email=%s", log_label, email)


async def _verify_code(
    redis: aioredis.Redis,
    *,
    key: str,
    email: str,
    code: str,
    log_label: str,
    consume: bool = True,
) -> None:
    """Generic verify-and-(optionally-)consume used by all email
    verification flows. On a code match: deletes the entry when
    ``consume=True`` (one-shot, the default), or leaves it untouched
    when ``consume=False`` (peek mode — caller intends to consume in
    a subsequent call).

    Mismatch path is identical regardless of ``consume``:
    increments ``attempts``; on exceeding the cap, deletes the entry
    and raises ``too_many_attempts``. Peek mode never weakens the
    brute-force defense.

    ``email`` is taken alongside ``key`` purely so log lines can
    record the human-readable address — the Redis key already
    embeds the email but with a flow-specific prefix that's noise in
    operational logs.
    """
    raw = await redis.get(key)
    if raw is None:
        raise InvalidVerificationCodeError(
            "Verification code expired or not found"
        )
    state = json.loads(raw)

    if state["code"] == code:
        if consume:
            await redis.delete(key)
        return

    attempts = state["attempts"] + 1
    if attempts >= settings.register_code_max_attempts:
        await redis.delete(key)
        logger.info(
            "%s code locked after max attempts: email=%s",
            log_label,
            email,
        )
        raise TooManyVerificationAttemptsError(
            "Too many incorrect verification attempts"
        )

    # Update with new attempt count, preserving the original TTL — we
    # don't want a wrong-guess to extend the code's validity.
    ttl = await redis.ttl(key)
    state["attempts"] = attempts
    await redis.set(key, json.dumps(state), ex=max(ttl, 1))
    raise InvalidVerificationCodeError("Incorrect verification code")


async def request_register_code(
    redis: aioredis.Redis, *, email: str, locale: Locale = "zh-CN"
) -> None:
    """Generate + send a registration verification code for ``email``.

    Raises :class:`~app.errors.CooldownError` if a previous code was
    sent within the cooldown window;
    :class:`~app.errors.EmailSendFailedError` if Aliyun rejects the
    send.
    """
    await _request_code(
        redis,
        key=email_verify_key(email),
        email=email,
        locale=locale,
        render=render_register_verification,
        log_label="register",
    )


async def verify_register_code(
    redis: aioredis.Redis,
    *,
    email: str,
    code: str,
    consume: bool = True,
) -> None:
    """Verify the pending registration code for ``email``.

    By default consumes the code on success (one-shot). Pass
    ``consume=False`` for peek mode, used by the unified email-OTP
    login flow when the caller wants to check code validity before
    deciding whether to log in (existing user) or hand off to the
    register endpoint (new user). See :func:`_verify_code` for the
    full failure-mode semantics.
    """
    await _verify_code(
        redis,
        key=email_verify_key(email),
        email=email,
        code=code,
        log_label="register",
        consume=consume,
    )


async def send_email_change_code(
    redis: aioredis.Redis, *, email: str, locale: Locale = "zh-CN"
) -> None:
    """Generate + send an email-change verification code to ``email``
    (the *new* address the user wants to switch to). Same failure
    modes as :func:`request_register_code`.

    Named ``send_*`` (rather than ``request_*``) so it doesn't
    collide with :func:`app.services.auth.email_change.request_email_change_code`
    — that one is the orchestrator (sudo + uniqueness pre-check + this
    dispatch). The two were originally same-named with an import alias;
    the rename here drops the alias."""
    await _request_code(
        redis,
        key=email_change_key(email),
        email=email,
        locale=locale,
        render=render_email_change_verification,
        log_label="email_change",
    )


async def verify_email_change_code(
    redis: aioredis.Redis, *, email: str, code: str
) -> None:
    """Verify and consume the pending email-change code for
    ``email``. See :func:`_verify_code` for failure modes."""
    await _verify_code(
        redis,
        key=email_change_key(email),
        email=email,
        code=code,
        log_label="email_change",
    )
