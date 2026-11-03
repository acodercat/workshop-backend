"""Constructing a session row.

Extracted from the ``POST /sessions`` handler once it gained a second
caller: the scheduled-task dispatcher, which stands up a session on a
user's behalf with no request in flight.

What had to be shared is the **tier resolution**. ``session.model_id`` is
not an audit field — the worker picks its LLM from it and the backend
prices the turn against it (``app/services/credits/rates.py``), so a
``model_id`` that came from a second, drifted copy of this mapping bills
against a rate card entry that may not exist. One resolution site, checked
against the same whitelist.

The function deliberately does **not** commit. Its two callers want
different transaction boundaries: the endpoint commits immediately, while
the dispatcher lets ``queue_user_message`` commit the session, the turn and
its own bookkeeping together.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import UnconfiguredTierError
from app.models.session import AgentType, Session, SessionStatus, Tier
from app.services.credits.rates import snapshot_rate
from app.services.session import connectors as session_connectors
from config import settings


def resolve_tier(tier: Tier) -> str:
    """The concrete ``model_id`` a capability tier runs as.

    "Runnable" spans **three** settings, and this is the one place that
    knows all three: the tier must map to a model
    (``session_tier_models``), that model must be permitted
    (``supported_session_model_ids``), and it must be priced
    (``credit_rates``, checked through
    :func:`~app.services.credits.rates.snapshot_rate`). A caller that
    checked only the first two would pass a tier that then fails at reserve
    time — which for a scheduled task means the fault surfaces unattended,
    hours later, as a string of skipped occurrences.

    Split out from :func:`build_session` so a caller that only wants to
    *validate* — the scheduled-task endpoints, which store a tier now and
    build the session much later — fails on the same input the builder
    would.

    Raises:
        UnconfiguredTierError: the tier maps to no model, or to one absent
            from ``settings.supported_session_model_ids``.
        ModelUnavailableError: the model has no rate-card entry, so a turn
            against it could not be priced.
    """
    model_id = settings.session_tier_models.get(tier)
    if model_id is None or model_id not in settings.supported_session_model_ids:
        # ``.value``, not ``!r``: this string is the English fallback a
        # client shows when it has no localized copy for the error, and
        # ``<Tier.ADVANCED: 'ADVANCED'>`` is not a sentence.
        raise UnconfiguredTierError(f"Capability tier {tier.value} is not configured.")
    # Raises ``ModelUnavailableError`` when the model is permitted but not
    # priced. The dispatcher already treats that as the same class of
    # operator-config gap; checking it here keeps one definition of runnable
    # rather than two that can drift.
    snapshot_rate(model_id)
    return model_id


async def build_session(
    db: AsyncSession,
    *,
    user_id: int,
    title: str | None,
    tier: Tier,
    agent_type: AgentType,
    thinking_enabled: bool,
    connector_ids: list[int],
    project_id: uuid.UUID | None,
) -> Session:
    """Add a new session to *db* and flush it, without committing.

    ``connector_ids`` must already be ownership-filtered, and ``project_id``
    must name a project the caller owns and has locked — both checks read
    a request's untrusted input and belong to whoever accepted it.

    Raises:
        UnconfiguredTierError: the tier maps to no model, or to one absent
            from ``settings.supported_session_model_ids``.
    """
    session = Session(
        title=title,
        user_id=user_id,
        model_id=resolve_tier(tier),
        tier=tier,
        agent_type=agent_type,
        thinking_enabled=thinking_enabled,
        status=SessionStatus.IDLE,
        project_id=project_id,
    )
    db.add(session)
    # The Connector rows key off ``session.id``, which a Python-side default
    # only assigns at flush.
    await db.flush()
    await session_connectors.replace(db, _session_id(session), connector_ids)
    return session


def _session_id(session: Session) -> uuid.UUID:
    """``session.id`` after flush, narrowed for the type checker."""
    assert session.id is not None
    return session.id
