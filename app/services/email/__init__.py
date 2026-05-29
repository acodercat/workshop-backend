"""Email-sending layer.

Aliyun DirectMail-backed. Two pieces:

- :mod:`.client` — async Aliyun ``SingleSendMail`` wrapper, the only
  module that talks to the SDK.
- :mod:`.templates` — locale-aware subject + HTML body for each kind
  of email we send (currently just registration verification).
"""

from app.services.email.client import EmailSendError, send_html_email
from app.services.email.templates import (
    render_account_activated,
    render_email_change_verification,
    render_register_verification,
)

__all__ = [
    "EmailSendError",
    "send_html_email",
    "render_account_activated",
    "render_register_verification",
    "render_email_change_verification",
]
