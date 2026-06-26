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


class PhoneTakenError(AppError):
    """The phone number is already registered. Mirror of
    :class:`EmailTakenError` for the SMS-OTP registration path."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "phone_taken"


class SmsSendFailedError(AppError):
    """Aliyun SMS dispatch failed. Mirror of
    :class:`EmailSendFailedError` for the phone verification path."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "sms_send_failed"


class NotRegisteredError(AppError):
    """Login attempt for an email with no account. The frontend steers
    the user to the (invite-gated) register tab rather than auto-creating."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "not_registered"


class InvalidInviteCodeError(AppError):
    """Invite code doesn't exist or is disabled."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invalid_invite_code"


class InviteExpiredError(AppError):
    """Invite code's ``expires_at`` has passed."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invite_expired"


class InviteExhaustedError(AppError):
    """Invite code has reached its ``max_uses`` cap."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invite_exhausted"


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


class SamePhoneError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "same_phone"


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


class FileSizeQuotaExceededError(AppError):
    """The upload would push the user past their per-user total-size quota
    (``max_session_files_bytes_per_user``). The individual file is fine on
    its own; it just doesn't fit the account's remaining storage headroom.
    The per-file size cap is enforced separately at the schema layer.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "file_size_quota_exceeded"


class FileCountQuotaExceededError(AppError):
    """The user has hit the per-user file-count backstop
    (``max_session_files_per_user``)."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "file_count_quota_exceeded"


class PaymentDisabledError(AppError):
    """The payment/recharge feature is off (``payment_enabled`` is False
    or Huifu credentials are unconfigured). 403 so the frontend hides the
    top-up surface rather than retrying."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "payment_disabled"


class PaymentAmountRangeError(AppError):
    """Requested top-up amount is outside ``[payment_min_cny,
    payment_max_cny]``. A client-side validation guard duplicated in the
    service layer so it holds regardless of caller."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "payment_amount_range"


class OrderNotFoundError(AppError):
    """No payment order with that id is owned by the caller (or it never
    existed). Folds the not-owned case into not-found so an order id can't
    be probed across users."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "order_not_found"


class PaymentProviderError(AppError):
    """The upstream payment provider returned an error / unparseable
    response when creating or querying an order. 502 so it reads as an
    upstream failure the client can retry, not a client error."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "payment_provider_error"


class CommissionDisabledError(AppError):
    """The referral-commission wallet is off (``commission_enabled`` is
    False). 403 so the frontend hides the withdraw/convert surface rather
    than retrying."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "commission_disabled"


class CommissionAmountRangeError(AppError):
    """Requested withdrawal / conversion amount is below the configured
    minimum (or too small to buy a whole credit). A client-side guard
    duplicated in the service layer so it holds regardless of caller."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "commission_amount_range"


class InsufficientCommissionError(AppError):
    """The withdrawable commission balance can't cover the requested
    withdrawal / conversion (frozen funds in the settlement hold don't
    count). 400 — the client should refresh the balance and retry with a
    smaller amount."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "insufficient_commission"


class WithdrawalNotFoundError(AppError):
    """No withdrawal request with that id is owned by the caller (or it
    never existed). Folds the not-owned case into not-found so an id can't
    be probed across users."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "withdrawal_not_found"
