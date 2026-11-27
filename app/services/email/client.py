"""SMTP client for transactional email (Lark Suite Mail).

One async function (:func:`send_html_email`) that takes a recipient,
subject, and HTML body and dispatches via :mod:`aiosmtplib`. All other
modules in :mod:`app.services.email` go through this seam — the SMTP
library isn't imported anywhere else.

Why a thin wrapper rather than calling ``aiosmtplib`` directly from the
caller:

- MIME assembly (``EmailMessage`` + UTF-8 subject + alias-formatted
  ``From``) is verbose and identical for every send; hide it behind
  one call.
- ``SMTPException`` (and TLS / DNS / socket errors) are foreign
  exception types. We surface them all as :class:`EmailSendError` so
  callers can ``except EmailSendError`` once.
- Provider config (host / port / SSL-vs-STARTTLS / login) is centralised
  in :mod:`config` — call sites stay clean.

Upstream cap (Lark Suite SMTP): 200 sends / 100 s and 6000 sends /
day per sender. What is sent here — operator-triggered notices — is far
below both, so there is no application-level throttle.
"""

import logging
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import aiosmtplib

from config import settings

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    """Raised when the SMTP server rejects or the network drops a send.

    Always carries a human-readable ``message``. ``code`` is the SMTP
    reply code (e.g. ``535`` for auth failure) when available, ``None``
    for transport errors. ``request_id`` is the local Message-Id we
    minted so logs on both sides can be correlated."""

    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.request_id = request_id


def _check_settings() -> None:
    if not settings.smtp_host:
        raise EmailSendError("SMTP host missing — set ``smtp_host`` (e.g. smtp.larksuite.com)")
    if not settings.smtp_username or not settings.smtp_password:
        raise EmailSendError(
            "SMTP credentials missing — set ``smtp_username`` and ``smtp_password``"
        )
    if not settings.smtp_from_address:
        raise EmailSendError(
            "SMTP sender address missing — set ``smtp_from_address`` "
            "(usually the same as ``smtp_username``)"
        )


def _build_message(*, to: str, subject: str, html_body: str, message_id: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.smtp_from_alias, settings.smtp_from_address))
    msg["To"] = to
    msg["Message-ID"] = message_id
    # Plain-text fallback first, then HTML alternative — keeps
    # text-only clients (and spam filters that downgrade HTML-only
    # mails) happy. The fallback is intentionally generic since the
    # whole point of the HTML body is the formatted code.
    msg.set_content("Your verification code is in the HTML version of this email.")
    msg.add_alternative(html_body, subtype="html")
    return msg


async def send_html_email(*, to: str, subject: str, html_body: str) -> str:
    """Send one transactional HTML email. Returns the Message-Id on
    success; raises :class:`EmailSendError` on failure.

    Transport is SSL-on-connect (port 465) when ``smtp_use_ssl`` is
    true, STARTTLS (port 587) otherwise. A fresh connection is opened
    per send and torn down at QUIT — transactional volumes are too low
    to make pooling worthwhile, and one-shot connects sidestep stale-
    socket and idle-timeout edges entirely.
    """
    _check_settings()

    # ``make_msgid`` needs a domain; derive it from the From address
    # so the Message-Id matches the sending domain and doesn't trip
    # reputation checks. Fall back to a random uuid if the address is
    # malformed (the SMTP send will surface the real error).
    domain = settings.smtp_from_address.split("@", 1)[-1] or uuid.uuid4().hex
    message_id = make_msgid(domain=domain)
    msg = _build_message(to=to, subject=subject, html_body=html_body, message_id=message_id)

    try:
        if settings.smtp_use_ssl:
            await aiosmtplib.send(
                msg,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=settings.smtp_password,
                use_tls=True,
                tls_context=ssl.create_default_context(),
                timeout=settings.smtp_timeout_seconds,
            )
        else:
            await aiosmtplib.send(
                msg,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=settings.smtp_password,
                start_tls=True,
                tls_context=ssl.create_default_context(),
                timeout=settings.smtp_timeout_seconds,
            )
    except aiosmtplib.SMTPException as exc:
        code = getattr(exc, "code", None)
        logger.warning(
            "SMTP send failed: code=%s message=%s message_id=%s to=%s",
            code,
            exc,
            message_id,
            to,
        )
        raise EmailSendError(
            message=str(exc) or "SMTP server rejected the send",
            code=code,
            request_id=message_id,
        ) from exc
    # Deliberately everything else, not a tuple of transport types. What the
    # caller does with a failure is the same whichever way the send broke:
    # report it and try again. Anything that escapes here instead reaches the
    # caller as an unexpected error with nothing to do about it. A malformed
    # `smtp_from_address` raising `ValueError` out of `aiosmtplib.send` is enough to reach that.
    except Exception as exc:
        logger.exception("SMTP transport failure to %s", to)
        raise EmailSendError(
            message=f"SMTP transport failure: {type(exc).__name__}: {exc}",
            request_id=message_id,
        ) from exc

    logger.info("SMTP sent: message_id=%s to=%s", message_id, to)
    return message_id
