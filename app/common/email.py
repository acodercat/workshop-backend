"""Email normalization shared by auth and profile schemas."""

from typing import Annotated

from pydantic import AfterValidator, EmailStr


def normalize_email(email: str) -> str:
    """Return the canonical storage/login form for an email address.

    Pydantic's ``EmailStr`` normalizes the domain but preserves local-part
    casing. The product treats email identity case-insensitively, so all
    auth flows store, look up, and key verification state by the lowercase
    address.
    """
    return email.strip().lower()


NormalizedEmail = Annotated[EmailStr, AfterValidator(normalize_email)]
