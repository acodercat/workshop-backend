"""Phone normalization shared by auth schemas.

China-mainland mobile only for now (11 digits, leading ``1[3-9]``);
strips spaces / dashes before validating so a pasted ``138 0013 8000``
or ``138-0013-8000`` is accepted and stored canonically.
"""

import re
from typing import Annotated

from pydantic import AfterValidator

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def normalize_phone(phone: str) -> str:
    """Return the canonical storage/login form for a phone number."""
    digits = re.sub(r"[\s-]", "", phone.strip())
    if not _PHONE_RE.match(digits):
        raise ValueError("Invalid China mainland mobile number")
    return digits


NormalizedPhone = Annotated[str, AfterValidator(normalize_phone)]
