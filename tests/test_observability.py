"""What Sentry is allowed to receive."""

from unittest.mock import patch

import pytest

from app import observability
from config import settings


def test_sentry_receives_no_request_body_and_no_frame_locals(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "sentry_dsn", "https://key@sentry.invalid/1")
    with patch("sentry_sdk.init") as init:
        observability._init_sentry()
    options = init.call_args.kwargs
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
