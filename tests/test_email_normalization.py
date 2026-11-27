from app.schemas.auth import PasswordResetRequest, RegisterRequest, VerificationCodeRequest


def test_auth_email_inputs_are_lowercased() -> None:
    assert VerificationCodeRequest(email="Alice@EXAMPLE.COM").email == "alice@example.com"
    assert (
        RegisterRequest(
            email="Alice@EXAMPLE.COM",
            password="hunter2hunter2",
            code="123456",
            name="Alice",
        ).email
        == "alice@example.com"
    )
    assert (
        PasswordResetRequest(
            email="Alice@EXAMPLE.COM",
            password="hunter2hunter2",
            code="123456",
        ).email
        == "alice@example.com"
    )
