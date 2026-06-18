"""Typed application error hierarchy.

Wire shape (Stripe-style nested envelope)::

    {"error": {"type": "<snake_case_kind>", "message": "<english>"}}

Service layer raises ``AppError`` subclasses directly; the endpoint
layer **does not** catch them. ``app_error_handler`` in
:mod:`app.exception_handlers` serializes any uncaught ``AppError``
into the envelope above. The class attribute ``status_code`` decides
the HTTP status — there is no reason → status translation table at
the endpoint layer.

``error_type`` is the **stable contract** with the frontend: clients
read ``error.type`` and resolve localized copy from their own i18n
catalog. ``message`` is an English developer-facing fallback —
terse, declarative, no "please ..." imperatives — shown only when
the client has no localized override.

All subclasses today are auth-flow errors; the module is at
``app/errors.py`` (not ``app/services/auth/errors.py``) so future
non-auth domains (sessions, shares, files) can add typed errors here
without a circular dependency.

To add a new error kind:

1. Add a subclass below with ``status_code`` + ``error_type``.
2. ``raise NewKindError("english fallback")`` from wherever the
   condition is detected.
3. Add a localized copy entry in the frontend's i18n catalog under
   the same ``error_type`` key.
"""

from typing import ClassVar

from fastapi import status


class AppError(Exception):
    """Base for every typed application error.

    Subclasses set ``status_code`` and ``error_type`` as class
    attributes; the constructor takes only the English ``message``.
    The :class:`ClassVar` annotations make this contract explicit to
    type-checkers — these are class-level constants, not instance
    state.
    """

    status_code: ClassVar[int] = status.HTTP_400_BAD_REQUEST
    error_type: ClassVar[str] = "bad_request"

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
    error_type = "invalid_verification_code"


class TooManyVerificationAttemptsError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "too_many_verification_attempts"


class IncorrectPasswordError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "incorrect_password"


class SameEmailError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "same_email"


class SamePasswordError(AppError):
    """Refused no-op: the new password matches the current one. A
    silent accept would still bump ``token_version`` and log every
    other session out for nothing."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "same_password"


class InsufficientCreditsError(AppError):
    """User's credit balance is below the reservation estimate for
    the requested turn. Surface 402 (the textbook code for "this
    request needs payment to proceed") so a generic 4xx handler in
    the frontend can route to the top-up flow without parsing the
    body."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    error_type = "insufficient_credits"


class ModelUnavailableError(AppError):
    """The session's ``model_id`` has no rate-card entry, so a turn can't
    be priced. An operator-config gap (a model in
    ``supported_session_model_ids`` but missing from ``credit_rates``), not
    user error — surface 503 so it reads as "temporarily can't serve this"
    rather than a generic 500, and stays diagnosable from the error type."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "model_unavailable"


class InvalidSkillBundleError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invalid_skill_bundle"


class InvalidSkillNameError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invalid_skill_name"


class SkillNameConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "skill_name_conflict"


class SkillBundleTooLargeError(AppError):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    error_type = "skill_bundle_too_large"


class SkillQuotaExceededError(AppError):
    """User already has the maximum allowed number of CUSTOM skills.

    Distinct from :class:`SkillBundleTooLargeError`: the individual
    bundle would have been accepted, but the user has hit the count
    cap on uploads.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "skill_quota_exceeded"


class SessionFileQuotaExceededError(AppError):
    """User has hit a per-user session-file upload quota — either the
    total-size cap or the file-count backstop.

    Matches the ``SessionFile`` DB model name. Distinct from
    :class:`FileTooLargeError`: the individual file would have been
    accepted on its own, but it would push the user's account over the
    quota. The per-file size cap is enforced separately in
    ``file_storage``.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "session_file_quota_exceeded"
