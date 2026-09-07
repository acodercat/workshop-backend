"""Field aliases shared across the console's request schemas.

``Annotated`` aliases — the idiomatic way to reuse a constraint across
models. They live here rather than in whichever resource module used
them first, so ``from app.schemas.admin.fields import Password`` names
the actual owner.
"""

from typing import Annotated

from pydantic import Field

from app.services.admin.staff import MIN_PASSWORD_CHARS

Password = Annotated[str, Field(min_length=MIN_PASSWORD_CHARS, max_length=128)]
"""Operator password. The floor comes from the service module so the
API, the provisioning script and the service cannot disagree."""
