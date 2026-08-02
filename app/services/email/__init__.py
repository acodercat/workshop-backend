"""Email-sending layer.

SMTP-backed (Lark Suite Mail by default). Two pieces:

- :mod:`.client` — async :mod:`aiosmtplib` wrapper, the only module
  that talks to the SMTP library.
- :mod:`.templates` — locale-aware subject + HTML body for each kind
  of email we send (registration + email-change verification).
"""

from app.services.email.client import EmailSendError, send_html_email
from app.services.email.templates import (
    render_account_activated,
    render_credits_topped_up,
    render_email_change_verification,
    render_register_verification,
)

__all__ = [
    "EmailSendError",
    "send_html_email",
    "render_account_activated",
    "render_credits_topped_up",
    "render_register_verification",
    "render_email_change_verification",
]
