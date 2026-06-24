import pytest
from pydantic import ValidationError

from app.common.phone import normalize_phone
from app.schemas.auth import CodeLoginRequest, RegisterCodeRequest


def test_normalize_phone_strips_separators() -> None:
    assert normalize_phone("138 0013 8000") == "13800138000"
    assert normalize_phone("138-0013-8000") == "13800138000"
    assert normalize_phone("  13800138000  ") == "13800138000"


@pytest.mark.parametrize(
    "bad",
    ["12345678901", "1380013800", "138001380000", "23800138000", "abcdefghijk"],
)
def test_normalize_phone_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(bad)


def test_identifier_body_resolves_phone_channel() -> None:
    body = RegisterCodeRequest(phone="138 0013 8000")
    assert body.channel == "phone"
    assert body.identifier == "13800138000"


def test_identifier_body_resolves_email_channel() -> None:
    body = CodeLoginRequest(email="Alice@EXAMPLE.COM", code="123456")
    assert body.channel == "email"
    assert body.identifier == "alice@example.com"


def test_identifier_body_requires_exactly_one() -> None:
    with pytest.raises(ValidationError):
        RegisterCodeRequest(email="a@example.com", phone="13800138000")
    with pytest.raises(ValidationError):
        RegisterCodeRequest()
