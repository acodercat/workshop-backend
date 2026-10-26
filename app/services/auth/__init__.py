"""Authentication-flow orchestration.

Sub-modules:

- :mod:`.verification` — Redis-backed verification code lifecycle
  (generate → send → validate → consume). Shared by every code flow;
  namespaced by Redis prefix so they don't collide.
- :mod:`.register` — register orchestration: redeem an invite code,
  create an ``ACTIVE`` user, log them straight in.
- :mod:`.invite` — invite-code creation + redemption.
- :mod:`.login` — login by verification code.
- :mod:`.sudo` — the sudo gate in front of credential changes:
  current password or a sudo code.
- :mod:`.email_change` — changing a signed-in user's email: sudo +
  verify code + bump ``token_version``.

The sudo and email-change exports stay submodule-scoped — they're
called from the user endpoints alone, so re-exporting at the package level
adds noise without saving work.
"""

from app.services.auth.invite import (
    claim_invite,
    create_invite_code,
    create_referral_code,
    get_or_create_referral_code,
    record_redemption,
)
from app.services.auth.login import login_by_code
from app.services.auth.register import register_user
from app.services.auth.verification import (
    request_verification_code,
    verify_verification_code,
)

__all__ = [
    "claim_invite",
    "create_invite_code",
    "create_referral_code",
    "get_or_create_referral_code",
    "login_by_code",
    "record_redemption",
    "register_user",
    "request_verification_code",
    "verify_verification_code",
]
