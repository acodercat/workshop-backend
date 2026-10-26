"""Field aliases shared across the console's request schemas.

``Annotated`` aliases — the idiomatic way to reuse a constraint across
models. They live here rather than in whichever resource module used
them first, so ``from app.schemas.admin.fields import Password`` names the
actual owner.
"""

from typing import Annotated

from pydantic import Field

from app.common.password import BcryptPassword
from app.services.admin.staff import MIN_PASSWORD_CHARS

Password = Annotated[BcryptPassword, Field(min_length=MIN_PASSWORD_CHARS)]
"""Operator password. The floor comes from the service module and the
ceiling is bcrypt's byte bound; the provisioning script validates through
this same alias, so the API, the script and the service cannot disagree."""
