"""Resolving a capability tier to the ``model_id`` a turn actually runs.

``session.model_id`` is not an audit field: the worker picks its LLM from
it and the backend prices the turn against it, so a tier that resolves to
nothing has to be refused rather than stored. One resolution site, shared
by the session builder and by the scheduled-task endpoints — which store a
tier now and build the session hours later, unattended, where the fault
would otherwise surface as a string of skipped occurrences.
"""

import pytest

from app.models.session import Tier
from app.services.session.creation import resolve_tier
from app.services.session.exceptions import UnconfiguredTierError
from config import settings


@pytest.mark.parametrize("tier", list(Tier))
def test_every_shipped_tier_resolves(tier):
    assert resolve_tier(tier) in settings.supported_session_model_ids


def test_a_tier_with_no_mapping_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "session_tier_models", {})
    with pytest.raises(UnconfiguredTierError):
        resolve_tier(Tier.STANDARD)


def test_a_tier_mapped_outside_the_whitelist_is_refused(monkeypatch):
    # The map and the whitelist are separate settings, so they can disagree
    # — and the one that decides what may run is the whitelist.
    monkeypatch.setattr(settings, "session_tier_models", {Tier.STANDARD: "model-nobody-configured"})
    with pytest.raises(UnconfiguredTierError):
        resolve_tier(Tier.STANDARD)
