"""Typed application error hierarchy.

Wire shape (Stripe-style nested envelope):

    {"error": {"type": "<snake_case_kind>", "message": "<english>"}}

Service layer raises ``AppError`` subclasses directly. The endpoint
layer lets them propagate; ``app_error_handler`` in
:mod:`app.exception_handlers` serializes them.

``error_type`` is the **stable contract** with the frontend — clients
read ``error.type`` and resolve localized copy from their own i18n
catalog. ``message`` is an English fallback shown only when the
client has no localized override; it is not the user-facing string in
normal flows.

To add a new error kind:

1. Add a subclass below with ``status_code`` + ``error_type``.
2. ``raise NewKindError("english fallback")`` from wherever the
   condition is detected.
3. Add a localized copy entry in the frontend's i18n catalog under
   the same ``error_type`` key.
"""

from fastapi import status


class AppError(Exception):
    """Base for every typed application error.

    Subclasses set ``status_code`` and ``error_type`` as class
    attributes; the constructor takes only the English ``message``.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    error_type: str = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EmailTakenError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "email_taken"


class AccountPendingError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    error_type = "account_pending"


class CooldownError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "cooldown"


class EmailSendFailedError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "email_send_failed"


class InvalidVerificationCodeError(AppError):
    """``not_found`` and ``mismatch`` collapse to one client-facing
    reason — recovery is identical (request a new code), so giving
    the client two distinct codes would be needless complexity."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invalid_code"


class TooManyVerificationAttemptsError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "verification_too_many_attempts"


class IncorrectPasswordError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "incorrect_password"


class SameEmailError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "same_email"
