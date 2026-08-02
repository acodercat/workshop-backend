"""One-way LITE → FULL mode upgrade riding a message send
(``enqueue._apply_agent_type_upgrade``).

The flip runs on the locked session row inside ``queue_user_message``,
so these cover the pure guard logic on an in-memory ``Session`` (no DB,
same construction style as ``test_lock_session``); the lock/atomicity
behaviour is integration-tier.

asyncio_mode = "auto" (see pyproject.toml); these are sync tests.
"""

import pytest

from app.errors import AgentTypeDowngradeError
from app.models.session import AgentType, Session
from app.services.session.enqueue import _apply_agent_type_upgrade


def test_none_keeps_current_mode() -> None:
    session = Session(agent_type=AgentType.LITE)
    _apply_agent_type_upgrade(session, None)
    assert session.agent_type is AgentType.LITE


def test_lite_to_full_upgrades() -> None:
    session = Session(agent_type=AgentType.LITE)
    _apply_agent_type_upgrade(session, AgentType.FULL)
    assert session.agent_type is AgentType.FULL


def test_same_value_is_idempotent() -> None:
    # A resend of an already-upgraded session must not error.
    session = Session(agent_type=AgentType.FULL)
    _apply_agent_type_upgrade(session, AgentType.FULL)
    assert session.agent_type is AgentType.FULL


def test_full_to_lite_rejects() -> None:
    session = Session(agent_type=AgentType.FULL)
    with pytest.raises(AgentTypeDowngradeError):
        _apply_agent_type_upgrade(session, AgentType.LITE)
    assert session.agent_type is AgentType.FULL
