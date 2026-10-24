"""A missing session and a dead share each answer with their own ``error_type``.

Both used to be a bare 404 typed ``not_found``. Minting a download link never
checks the object, so these two are the only 404s a download can meet — and
with one type for both, the frontend told a visitor on a revoked share that
the file had been deleted.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.public.session import _get_or_404
from app.api.public.share import _resolve_share
from app.errors import SessionNotFoundError, ShareNotFoundError


def _db_finding_nothing() -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.first.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


async def test_a_missing_session_is_session_not_found() -> None:
    with pytest.raises(SessionNotFoundError) as excinfo:
        await _get_or_404(_db_finding_nothing(), "00000000-0000-0000-0000-000000000000", 42)

    assert excinfo.value.error_type == "session_not_found"


async def test_a_dead_share_is_share_not_found() -> None:
    with pytest.raises(ShareNotFoundError) as excinfo:
        await _resolve_share(_db_finding_nothing(), "revoked-token")

    assert excinfo.value.error_type == "share_not_found"
