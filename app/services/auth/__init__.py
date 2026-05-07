"""Authentication-flow orchestration.

Sub-modules:

- :mod:`.verification` — Redis-backed registration verification code
  lifecycle (generate → send → validate → consume).
- :mod:`.register` — register orchestration: validate code, create
  user with ``status=PENDING``.
"""

from app.services.auth.register import RegisterError, register_user
from app.services.auth.verification import (
    VerificationError,
    request_register_code,
    verify_register_code,
)

__all__ = [
    "RegisterError",
    "VerificationError",
    "register_user",
    "request_register_code",
    "verify_register_code",
]
