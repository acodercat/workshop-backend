"""Email-sending layer.

SMTP-backed (Lark Suite Mail by default). Two pieces:

- :mod:`.client` — async :mod:`aiosmtplib` wrapper, the only module
  that talks to the SMTP library.
- :mod:`.templates` — locale-aware subject + HTML body for each kind
  of email we send (account activation, credit top-up notices).

Verification codes are not sent from here: sign-in is federated to the
identity provider, which sends its own.
"""

from app.services.email.client import EmailSendError, send_html_email
from app.services.email.templates import (
    render_account_activated,
    render_credits_topped_up,
)

__all__ = [
    "EmailSendError",
    "send_html_email",
    "render_account_activated",
    "render_credits_topped_up",
]
