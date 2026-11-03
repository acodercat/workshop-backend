"""One-way LITE → FULL mode upgrade
(``agent_type.upgrade_agent_type``).

The flip runs on the locked session row inside ``queue_user_message``,
so these cover the pure guard logic on an in-memory ``Session`` (no DB,
same construction style as ``test_lock_session``); the lock/atomicity
behaviour is integration-tier.

asyncio_mode = "auto" (see pyproject.toml); these are sync tests.
"""

import pytest

from app.errors import AgentTypeDowngradeError
from app.models.session import AgentType, Session
from app.services.session.agent_type import upgrade_agent_type


def test_none_keeps_current_mode() -> None:
    session = Session(agent_type=AgentType.LITE)
    upgrade_agent_type(session, None)
    assert session.agent_type is AgentType.LITE


def test_lite_to_full_upgrades() -> None:
    session = Session(agent_type=AgentType.LITE)
    upgrade_agent_type(session, AgentType.FULL)
    assert session.agent_type is AgentType.FULL


def test_same_value_is_idempotent() -> None:
    # A resend of an already-upgraded session must not error.
    session = Session(agent_type=AgentType.FULL)
    upgrade_agent_type(session, AgentType.FULL)
    assert session.agent_type is AgentType.FULL


def test_full_to_lite_rejects() -> None:
    session = Session(agent_type=AgentType.FULL)
    with pytest.raises(AgentTypeDowngradeError):
        upgrade_agent_type(session, AgentType.LITE)
    assert session.agent_type is AgentType.FULL
