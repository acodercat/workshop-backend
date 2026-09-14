"""Typed application error hierarchy.

Wire shape (Stripe-style nested envelope)::

    {"error": {"type": "<snake_case_kind>", "message": "<english>",
               "details": <optional structured payload>}}

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

Subclasses span every domain — auth, credits, payments, commission,
skills, check-in, sessions. The module lives at ``app/errors.py`` (not
under any one domain package) precisely so any of them can add typed
errors here without a circular dependency.

How many kinds is the right number: one per distinct thing the user
would *do next*, not one per place a check fails. Two conditions that
send someone to the same fix belong under one type — see
:class:`InvalidVerificationCodeError`, which folds two. Two that send
them to different fixes must not, or the client is left writing copy
vague enough to cover both, which is copy that helps with neither.

To add a new error kind:

1. Add a subclass below with ``status_code`` + ``error_type``.
2. ``raise NewKindError("english fallback")`` from wherever the
   condition is detected. If the localized sentence needs a number to
   be actionable, pass ``details`` and let the class build both from
   the same values.
3. Add a localized copy entry in the frontend's i18n catalog under
   the same ``error_type`` key, interpolating whatever ``details``
   carries.
"""

from typing import Any, ClassVar

from fastapi import status


class AppError(Exception):
    """Base for every typed application error.

    Subclasses set ``status_code`` and ``error_type`` as class
    attributes; the constructor takes the English ``message`` and, where
    the localized copy needs to state a number, ``details``. The
    :class:`ClassVar` annotations make this contract explicit to
    type-checkers — these are class-level constants, not instance
    state.

    ``details`` is the same envelope slot the request-validation handler
    fills, and it exists for the same reason: some refusals are only
    actionable with their numbers in them. A client renders its own
    sentence from ``type`` and interpolates these — so keep the keys
    stable, name them for what they mean rather than where they came
    from, and put nothing in them a user should not read.
    """

    status_code: ClassVar[int] = status.HTTP_400_BAD_REQUEST
    error_type: ClassVar[str] = "bad_request"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


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


class SkillFileTypeError(AppError):
    """The uploaded file is not one of the accepted extensions. Caught
    before anything is read, so it says nothing about the contents."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "skill_file_type"


class SkillBundleUnreadableError(AppError):
    """The archive, or the ``SKILL.md`` in it, could not be read at all —
    not a zip, truncated, or text that is not UTF-8. Re-export and retry."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "skill_bundle_unreadable"


class SkillMdMissingError(AppError):
    """No ``SKILL.md`` where one has to be, or more than one to choose
    from. About the shape of the archive, not the content of the file."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "skill_md_missing"


class SkillFrontmatterInvalidError(AppError):
    """``SKILL.md`` was read, and its frontmatter does not carry what the
    Agent Skills spec requires — in practice, a ``name``."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "skill_frontmatter_invalid"


class InvalidSkillNameError(AppError):
    """``name`` is present and not a usable slug. Separate from
    :class:`SkillFrontmatterInvalidError` because the field is there and
    the fix is to rewrite it, not to add it."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invalid_skill_name"


class SkillBodyTooLargeError(AppError):
    """``SKILL.md``'s body is over the cap.

    Carries ``size`` and ``limit`` because the fix is a judgement about
    how much to move out, and "too large" without the two numbers does
    not support one. The body is injected into the agent's context and
    survives compaction, which is why there is a cap at all.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "skill_body_too_large"

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"SKILL.md body is too large ({size} bytes; max {limit}). "
            "Keep SKILL.md an overview and move bulk content into "
            "reference files linked from it.",
            details={"size": size, "limit": limit},
        )


class InvalidSkillBundleError(AppError):
    """A bundle refused for a reason its uploader cannot act on.

    What is left after the kinds above take the actionable failures: a
    zip bomb, an absurd compression ratio, a path that escapes its own
    archive, a symlink. Nobody assembling a skill by hand produces one,
    so the client says only that the file was rejected — a message
    explaining *how* the archive is hostile is a message written for
    whoever built it that way.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invalid_skill_bundle"


class SkillNameConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "skill_name_conflict"


class SkillBundleTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    error_type = "skill_bundle_too_large"


class SkillQuotaExceededError(AppError):
    """User already has the maximum allowed number of CUSTOM skills.

    Distinct from :class:`SkillBundleTooLargeError`: the individual
    bundle would have been accepted, but the user has hit the count
    cap on uploads.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "skill_quota_exceeded"


class InvalidConnectorUrlError(AppError):
    """The Connector URL isn't a usable public https endpoint — wrong scheme,
    unresolvable host, or it resolves to a private / loopback / link-local
    address (the SSRF guard). 400: the client must supply a different URL."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invalid_connector_url"


class ConnectorUnreachableError(AppError):
    """Couldn't complete the MCP handshake with the remote server (connect /
    initialize / list-tools failed or timed out, or auth was rejected). 502 —
    reads as an upstream failure the user can retry after fixing the server."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "connector_unreachable"


class ConnectorUrlConflictError(AppError):
    """The user already has a Connector pointing at this server
    (``uq_mcp_server_user_id_url``).

    The server is what a Connector *is*; the name is only the label the user
    put on it. Two rows at one URL are the same Connector added twice however
    they are named, and two rows sharing a name are not necessarily anything —
    which is why the constraint moved here and names are now free to repeat."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "connector_url_conflict"


class ConnectorCredentialUnreadableError(AppError):
    """A secret stored on the Connector will not decrypt — the encryption key
    has moved on, or the column is damaged. 409: the request is fine, the row
    is not, and the only remedy is to supply the secret again."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "connector_credential_unreadable"


class ConnectorNotOAuthError(AppError):
    """An OAuth step was asked of a Connector that authenticates some other way.
    400: the request describes something the row is not."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "connector_not_oauth"


class ConnectorNotAuthorizedError(AppError):
    """Asked to connect an OAuth Connector that holds no grant. 400: connecting
    is the wrong verb for it — authorizing is what it still needs."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "connector_not_authorized"


class ConnectorAuthorizationExpiredError(AppError):
    """The authorization attempt this request belongs to is no longer live —
    its state has lapsed, or the Connector has stopped waiting. 400: starting a
    new one is the whole remedy, and always available."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "connector_authorization_expired"


class ConnectorAuthorizationSupersededError(AppError):
    """A newer authorization was started for this Connector, so this callback
    belongs to an attempt the user has already abandoned. Distinct from
    :class:`ConnectorAuthorizationExpiredError` because the remedy is the
    opposite: finish the *other* one rather than start another."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "connector_authorization_superseded"


class ConnectorChangedDuringAuthorizationError(AppError):
    """The Connector's OAuth client was replaced while the user was away at the
    provider, so this grant would land on a row it does not describe. 400: the
    attempt is void and a fresh one will succeed."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "connector_changed_during_authorization"


class ConnectorOAuthClientUnusableError(AppError):
    """The stored OAuth client cannot be used against the authorization server
    and cannot be replaced either — the user registered it themselves, so
    nothing here may re-mint it. 400: only a new Connector helps."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "connector_oauth_client_unusable"


class ConnectorOAuthChangeUnsupportedError(AppError):
    """A PATCH would move a Connector into, or an OAuth Connector's URL out of,
    the shape its grant was issued against. 400: an authorization is bound to
    the server it was granted for, so this is a new Connector, not an edit."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "connector_oauth_change_unsupported"


class OAuthNotConfiguredError(AppError):
    """This deployment has no OAuth redirect URI, so it cannot run the flow at
    all. 500 rather than the 502 its neighbours use: those name a third party
    having a bad day, and a caller reads them as "retry, or go fix your OAuth
    app". Nothing here is the caller's, and no amount of retrying helps."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type = "oauth_not_configured"


class ConnectorCatalogEntryNotFoundError(AppError):
    """No such entry in the official Connector directory — the id is unknown or
    the entry has been unpublished. 404 rather than 403 for an unpublished one:
    an unlisted entry should read as absent, not as "exists but withheld"."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "connector_catalog_entry_not_found"


class ConnectorOAuthError(AppError):
    """An OAuth step failed — metadata discovery, dynamic client registration,
    or the authorization-code exchange. 502: reads as an upstream/provider
    failure the user can retry after fixing the server or their OAuth app."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "connector_oauth_failed"


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


class CheckInDisabledError(AppError):
    """Daily check-in is off (``checkin_reward_credits`` is 0). 403 so the
    frontend hides the check-in surface rather than retrying."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "check_in_disabled"


class AlreadyCheckedInError(AppError):
    """The user already claimed today's check-in. 409 (conflict) — the
    client refreshes the status and shows the already-claimed state. The
    double-guard (``check_in`` UNIQUE + ``CHECKIN`` ledger idempotency
    key) makes this race-safe."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "already_checked_in"


class AgentTypeDowngradeError(AppError):
    """A message send carried a FULL → LITE ``agent_type``. Only the
    LITE → FULL upgrade exists (a downgrade would strand the session's
    sandbox and files); the UI never offers it, so this is a client
    programming error — 400."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "agent_type_downgrade"


class StaffAuthenticationError(AppError):
    """The admin console could not identify the caller — absent, malformed,
    expired, wrongly-scoped or revoked token, or a deactivated account. All
    of those collapse into this one error on purpose: distinguishing them
    hands an attacker a probe against the highest-privilege surface in the
    system. The console reads it as "sign in again"."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "staff_unauthenticated"


class StaffCredentialsError(AppError):
    """Admin-console sign-in failed. A wrong password and a ``DISABLED``
    account raise the *same* error: a former operator probing the console
    should not be able to confirm their account still exists."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "staff_invalid_credentials"


class StaffNotFoundError(AppError):
    """No staff account with that id."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "staff_not_found"


class StaffUsernameTakenError(AppError):
    """A staff account with that username already exists. Usernames are the
    console's login key, so this is a conflict rather than a validation
    error — the caller picks another, it isn't a malformed request."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "staff_username_taken"


class LastActiveStaffError(AppError):
    """The change would leave the console with no ``ACTIVE`` staff account
    — a full lockout, recoverable only by someone with database
    credentials running ``scripts/manage_staff.py``. Add or re-enable
    another account first."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "staff_last_active"


class UserNotFoundError(AppError):
    """No user account with that id (admin-console lookup)."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "user_not_found"


class SessionNotFoundError(AppError):
    """No session with that id (admin-console lookup — unlike the
    customer surface, soft-deleted sessions are still found here, so
    this fires only for ids that never existed)."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "session_not_found"


class FeedbackNotFoundError(AppError):
    """No feedback submission with that id (admin-console lookup)."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "feedback_not_found"


class SkillNotFoundError(AppError):
    """No skill with that id (admin-console lookup — unlike the
    customer surface, the console sees every origin, so this fires only
    for ids that never existed)."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "skill_not_found"


class PrivateSkillBundleError(AppError):
    """The console asked for a bundle that is not the platform's.

    403 rather than 404: the row is on the operator's screen, so
    pretending it does not exist would read as a bug. What is refused is
    the *bytes* — a ``CUSTOM`` bundle is a user's private file, the same
    class of thing as a conversation, and the console reads neither.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "private_skill_bundle"


class PrivateSkillLifecycleError(AppError):
    """The console tried to publish or retire a user's own skill.

    The same boundary :class:`PrivateSkillBundleError` draws, one step
    further in: not only are a ``CUSTOM`` bundle's bytes the user's, so
    is whether it exists for them. Retiring one would leave it where its
    owner cannot see, edit, delete or stop paying quota for it, and no
    surface would explain where it went. 403 for the same reason: the
    row is on the operator's screen.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "private_skill_lifecycle"


class CategoryNotFoundError(AppError):
    """No category with that id (admin-console lookup, both taxonomies).

    One error for ``skill_category`` and ``connector_category`` alike: they
    are separate tables, so passing one's id where the other is expected
    already lands here, and the operator's next move — pick a real id out of
    ``GET /{skill,connector}-categories`` — is the same either way. The
    message names the taxonomy; the type does not need to."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "category_not_found"


class ConnectorCatalogUrlTakenError(AppError):
    """The catalog already offers this URL. Two entries pointing at one
    server are the same Connector offered twice, whatever they are
    named, so ``url`` carries the table's unique constraint — this is a
    conflict with an existing row, not a malformed request."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "connector_catalog_url_taken"


class AdjustmentAlreadyAppliedError(AppError):
    """A credit adjustment with this idempotency key was already applied.
    409 so the console shows "already applied" instead of silently
    double-granting on a retried form submission."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "adjustment_already_applied"


class ScheduleActiveLimitError(AppError):
    """The caller is at the per-account cap on *active* schedules.

    Its own kind rather than a bare 403 because what the reader must do
    next is specific and not "try again": pause or delete one of the
    schedules they already have. A status-derived ``forbidden`` falls
    through the frontend catalog to "something went wrong, try again
    later", which is advice that can never work here."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "schedule_active_limit"

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"At most {limit} schedules may be active at once.",
            details={"limit": limit},
        )


class ScheduleLimitError(AppError):
    """The caller is at the per-account cap on schedules of any status.

    Distinct from the active cap because the remedy differs: this one is
    cleared only by deleting, not by pausing. A one-shot frees its active
    slot the moment it fires, so a user can be far from the active cap and
    still be at this one."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "schedule_limit"

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"At most {limit} schedules per account; delete some to make room.",
            details={"limit": limit},
        )


class ScheduleRunTimePastError(AppError):
    """The recurrence names an instant that has already gone.

    Reachable only for a one-shot (a recurring schedule always has a next
    occurrence), and by far the likeliest mistake in the create form:
    today's date with a time earlier than now. Worth saying so rather than
    "the request was invalid"."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "schedule_run_time_past"


class ScheduleTimezoneUnavailableError(AppError):
    """The schedule's stored timezone is not in this deployment's tz data.

    Stored zones are validated on the way in, so reaching this means the two
    sides disagree — the row was written against a tz database that has
    since been rolled back, or the API and the background process run
    different images. The remedy belongs to an operator (align the images)
    or to the owner (pick a zone both sides know), and a 500 would hide
    which."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "schedule_timezone_unavailable"

    def __init__(self, timezone: str) -> None:
        super().__init__(
            f"Timezone {timezone!r} is not available on this deployment.",
            details={"timezone": timezone},
        )


class ScheduleCompletedError(AppError):
    """A completed schedule was asked to pause.

    ``COMPLETED`` is terminal: a one-shot that has had its turn, with
    ``next_run_at`` NULL to say there is nothing left. Pausing it would
    strand the row in a state the model does not describe — PAUSED and
    NULL — which no resume can leave, and would replace the "this already
    ran" the owner was reading with nothing. Editing the recurrence is what
    brings such a schedule back, because that gives it something to do."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "schedule_completed"

    def __init__(self) -> None:
        super().__init__("This schedule has already completed; edit its schedule to run it again.")


class FileNotInSessionError(AppError):
    """A ``file_key`` was requested against a session it is not bound to.

    Raised by the download and preview paths. 403 rather than 404: the
    caller knows the key exists, and a different answer for "no such file"
    would let the endpoint probe which keys do."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "file_not_in_session"


class FileNotOwnedError(AppError):
    """A ``file_key`` the caller tried to store is held by another user.

    The guard that keeps a share viewer — who legitimately sees the keys of
    a shared session's files — from binding one to a session or schedule of
    their own, and so from minting download URLs after the share ends."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "file_not_owned"


class FileNotUploadedError(AppError):
    """A ``file_key`` with no object behind it — the presigned upload never
    completed. Raised where the real size is looked up, which is the one
    moment the object provably exists or does not."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "file_not_uploaded"


class FileTooLargeError(AppError):
    """An object over the policy's per-file cap, whether declared at mint
    time or measured at bind time. Carries both numbers: the sentence the
    client renders has to say how far over."""

    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    error_type = "file_too_large"

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"File too large: {size_bytes} bytes exceeds the {max_bytes}-byte cap",
            details={"size_bytes": size_bytes, "max_bytes": max_bytes},
        )


class FileRetentionFailedError(AppError):
    """Object storage would not take an upload off the orphan-reaping rule.

    Raised only where keeping the file is a precondition of the save: a
    scheduled task may not fire for days, and an attachment still tagged when
    the ~1-day lifecycle rule runs is deleted underneath a task that reports
    it saved. 502 — the storage service failed, and the same request can
    simply be sent again."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "file_retention_failed"


class ConnectorNotOwnedError(AppError):
    """A Connector selection named an id the caller does not own.

    Refused rather than dropped: an unknown id is a client bug, and storing
    it would leave a foreign id bound to this user's session or schedule.
    400 rather than the 404 a single-Connector path gives, because the id is
    one of several in a body the client composed and naming the refusal is
    the useful answer."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "connector_not_owned"
