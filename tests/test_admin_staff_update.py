"""``PATCH /staff/{id}`` writes no audit row when nothing changes.

Every other console PATCH already skips a no-op; this one wrote a
``staff.update`` row with an empty payload — and, with ``status`` present,
locked every active account to check a change that was not happening.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from app.api.admin import staff as endpoint
from app.models.staff import Staff, StaffStatus
from app.schemas.admin.staff import StaffUpdate


async def test_an_unchanged_patch_audits_nothing(monkeypatch) -> None:
    account = Staff(
        id=3,
        username="alice",
        name="Alice",
        password_hash="x",
        status=StaffStatus.ACTIVE,
        token_version=0,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(endpoint, "_get_or_404", AsyncMock(return_value=account))
    audit = MagicMock()
    monkeypatch.setattr(endpoint, "audit", audit)

    await endpoint.update_staff(
        MagicMock(), 3, StaffUpdate(name="Alice", status=StaffStatus.ACTIVE)
    )

    audit.assert_not_called()
