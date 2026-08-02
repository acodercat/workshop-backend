from app.schemas.auth import CodeLoginRequest, RegisterCodeRequest, RegisterRequest
from app.schemas.user import EmailChangeCodeRequest, EmailChangeRequest


def test_auth_email_inputs_are_lowercased() -> None:
    assert RegisterCodeRequest(email="Alice@EXAMPLE.COM").email == "alice@example.com"
    assert RegisterRequest(
        email="Alice@EXAMPLE.COM", code="123456", name="Alice",
        invite_code="abc123",
    ).email == "alice@example.com"
    assert CodeLoginRequest(
        email="Alice@EXAMPLE.COM", code="123456",
    ).email == "alice@example.com"


def test_email_change_inputs_are_lowercased() -> None:
    assert EmailChangeCodeRequest(
        new_email="Alice@EXAMPLE.COM",
        current_password="pw",
    ).new_email == "alice@example.com"
    assert EmailChangeRequest(
        new_email="Alice@EXAMPLE.COM", code="123456",
    ).new_email == "alice@example.com"
