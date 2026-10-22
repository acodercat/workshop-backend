"""Settings refuse at boot what would otherwise fail every request, or sign
every token with a key anyone has."""

import pytest
from pydantic import ValidationError

from config import Settings

# Every rate limit, so one added later is checked without being listed here.
_RATE_LIMITS = [name for name in Settings.model_fields if name.startswith("rate_limit_")]


@pytest.mark.parametrize("secret_key", ["change-me-to-a-random-secret-key", "too-short"])
def test_a_placeholder_or_short_secret_key_is_refused(secret_key):
    with pytest.raises(ValidationError, match="secret_key"):
        Settings(secret_key=secret_key)


@pytest.mark.parametrize("field", _RATE_LIMITS)
def test_a_malformed_rate_limit_is_refused(field):
    with pytest.raises(ValidationError, match=field):
        Settings(**{field: "5/minuet"})


@pytest.mark.parametrize("field", _RATE_LIMITS)
def test_each_default_rate_limit_is_accepted(field):
    default = Settings.model_fields[field].default
    assert getattr(Settings(**{field: default}), field) == default
