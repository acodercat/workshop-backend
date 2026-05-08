"""Authentication-flow orchestration.

Sub-modules:

- :mod:`.verification` — Redis-backed verification code lifecycle
  (generate → send → validate → consume). Shared between registration
  and email-change flows; namespaced by Redis prefix so they don't
  collide.
- :mod:`.register` — register orchestration: validate code, create
  user with ``status=PENDING``.
- :mod:`.email_change` — change-email orchestration for an
  authenticated user: sudo + verify code + bump ``token_version``.

Email-change exports stay submodule-scoped (``from app.services.auth
.email_change import ...``) to avoid shadowing the same-named
``request_email_change_code`` in :mod:`.verification`.
"""

from app.services.auth.register import register_user
from app.services.auth.verification import (
    request_register_code,
    verify_register_code,
)

__all__ = [
    "register_user",
    "request_register_code",
    "verify_register_code",
]
