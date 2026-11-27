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

Subclasses span every domain, one section of this file each. The module
lives at ``app/errors.py`` (not under any one domain package) precisely so
any of them can add typed errors here without a circular dependency.

How many kinds is the right number: one per distinct thing the user
would *do next*, not one per place a check fails. Two conditions that
send someone to the same fix belong under one type — see
:class:`InvalidCredentialsError`, which folds two. Two that send
them to different fixes must not, or the client is left writing copy
vague enough to cover both, which is copy that helps with neither.

To add a new error kind:

1. Add a subclass to its domain's section with ``status_code`` +
   ``error_type``.
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

# ── Base ────────────────────────────────────────────────────────────────────


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


# ── Sign-in and account ─────────────────────────────────────────────────────


class UserAuthenticationError(AppError):
    """The API could not identify the caller — a malformed, expired,
    wrongly-scoped or revoked token, or an account that is not active. One
    error for all of them: telling them apart would let a caller probe
    which accounts exist and which are locked. A client reads it as "sign
    in again".

    Its ``type`` is the generic ``unauthorized`` a bare 401 carries, the
    value clients already switch on.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "unauthorized"


class InvalidCredentialsError(AppError):
    """Sign-in refused by the identity provider: wrong password or no such
    account. The provider answers both the same way and so do we, so a
    refusal never says whether an account exists."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "invalid_credentials"


class AccountInactiveError(AppError):
    """The account may not sign in: disabled or suspended here, or not yet
    activated at the identity provider. Said plainly, unlike a wrong
    password — the caller has just proved the account is theirs."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "account_inactive"


class AccountFrozenError(AppError):
    """The identity provider has frozen the account. Distinct from
    :class:`AccountInactiveError` because the way out differs: nothing the
    user does here or there lifts it — they contact whoever froze it."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "account_frozen"


class AccountLockedError(AppError):
    """Too many failed sign-ins; the identity provider has locked the
    account for a while. The fix is to wait, which is why it is a 429
    and not the 401 a wrong password gets."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "account_locked"


class PasswordPolicyError(AppError):
    """A new password the identity provider would not accept. Checked here
    first because the provider answers a bad one with nothing a user can
    act on. ``details`` carries the bounds, in bytes."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "password_policy"


class IdentityProviderUnavailableError(AppError):
    """The identity provider could not be reached, or answered with
    something that is not an answer. Nothing is wrong with the request —
    trying again later is the whole remedy.

    A 503 and never a 401: a client that took this for a dead session
    would sign the user out over somebody else's outage."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "identity_provider_unavailable"


class IdentityProviderRejectedError(AppError):
    """The identity provider understood an account request — a
    registration, a code, a password reset — and declined it.

    One type for every such refusal because the provider's reasons are its
    own vocabulary, not a contract of ours: ``details["code"]`` carries its
    code for whoever reads the logs, and a reason earns a type of its own
    here once it is known to need different copy."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "identity_provider_rejected"


class IdentityProviderRateLimitedError(AppError):
    """The identity provider is throttling this caller."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "identity_provider_rate_limited"


class IncorrectPasswordError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "incorrect_password"


class SamePasswordError(AppError):
    """Refused no-op: the new password matches the current one. A
    silent accept would still bump ``token_version`` and log every
    other session out for nothing."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "same_password"


# ── Console staff ───────────────────────────────────────────────────────────


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


# ── Credits ─────────────────────────────────────────────────────────────────


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


class AdjustmentAlreadyAppliedError(AppError):
    """A credit adjustment with this idempotency key was already applied.
    409 so the console shows "already applied" instead of silently
    double-granting on a retried form submission."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "adjustment_already_applied"


# ── Check-in ────────────────────────────────────────────────────────────────


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


# ── Sessions ────────────────────────────────────────────────────────────────


class SessionNotFoundError(AppError):
    """No session with that id that the caller may see.

    On the customer surface that covers soft-deleted sessions and other
    users' ids too — one answer, so ownership is not leaked. The admin
    console still finds soft-deleted sessions, so there it fires only for
    ids that never existed."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "session_not_found"


class SessionPinLimitError(AppError):
    """The caller already has the most pinned sessions allowed. Cleared by
    unpinning one."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "session_pin_limit"

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"At most {limit} pinned sessions; unpin one to pin another.",
            details={"limit": limit},
        )


class AgentTypeDowngradeError(AppError):
    """A message send carried a FULL → LITE ``agent_type``. Only the
    LITE → FULL upgrade exists (a downgrade would strand the session's
    sandbox and files); the UI never offers it, so this is a client
    programming error — 400."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "agent_type_downgrade"


class UnconfiguredTierError(AppError):
    """A capability tier resolves to no model this deployment can run —
    absent from ``settings.session_tier_models``, or naming a model outside
    ``settings.supported_session_model_ids``. A configuration gap rather
    than a caller mistake, but one only a request for that tier finds."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "tier_unconfigured"


class GenerationInProgressError(AppError):
    """The session's last turn is still running — refused for a new message
    and for a delete alike. 409: wait for it, or stop it."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "generation_in_progress"


class ConcurrentTaskLimitError(AppError):
    """The user already has ``settings.max_concurrent_running_tasks`` tasks
    running. Its own kind rather than a status-derived 429, because the
    remedy is to let a running task finish or stop it, not to slow down —
    and the slowapi 429 already owns ``rate_limited``."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "concurrent_task_limit"

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"At most {limit} tasks may run at once.",
            details={"limit": limit},
        )


class SessionIdleError(AppError):
    """A message was sent to wait in a queue that is not waiting on anything:
    no turn runs and no queued message is about to go out — the turn ended
    between the client's last look and this request. 409: nothing is in the
    way any more, so the message can simply be sent."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "session_idle"


class MessageNotFoundError(AppError):
    """No message with that id in the session — the session itself has
    already been resolved as the caller's."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "message_not_found"


class NotAssistantMessageError(AppError):
    """Feedback was sent for a message the agent did not write. The UI
    offers ratings on assistant messages only, so this is a client
    programming error — 400."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "not_assistant_message"


class FeedbackNotFoundError(AppError):
    """No feedback submission with that id (admin-console lookup)."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "feedback_not_found"


class QueuedMessageNotFoundError(AppError):
    """No waiting message with that id in the session. The usual cause is
    not a bad id: the message went out, and stopped being a queued one."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "queued_message_not_found"


class QueuedMessageLimitError(AppError):
    """The session's queue is full. Cleared as the queue drains, or by
    deleting a waiting message."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "queued_message_limit"

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"At most {limit} messages may wait in one session.",
            details={"limit": limit},
        )


class ShareNotFoundError(AppError):
    """No live share behind that token: never created, revoked, or its
    session is gone. One type for all of them, because the visitor's next
    step is the same — ask the owner for a new link."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "share_not_found"


# ── Files ───────────────────────────────────────────────────────────────────


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


class FileNotUploadedError(AppError):
    """A ``file_key`` with no object behind it — the presigned upload never
    completed. Raised where the real size is looked up, which is the one
    moment the object provably exists or does not."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "file_not_uploaded"


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
    a shared session's files — from binding one to a session or project of
    their own, and so from minting download URLs after the share ends."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "file_not_owned"


class FileRetentionFailedError(AppError):
    """Object storage would not take an upload off the orphan-reaping rule.

    Raised only where keeping the file is a precondition of the save: a
    project file may not be read for weeks, and one still tagged when the
    ~1-day lifecycle rule runs is deleted underneath a project that reports
    it added. 502 — the storage service failed, and the same request can
    simply be sent again."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "file_retention_failed"


# ── Projects ────────────────────────────────────────────────────────────────


class ProjectNotFoundError(AppError):
    """No live project with that id that the caller owns. One answer for a
    deleted project, someone else's and one that never existed, so ids are
    not leaked."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "project_not_found"


class ProjectLimitError(AppError):
    """The caller is at the per-account cap on live projects. Cleared by
    deleting one."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "project_limit"

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"At most {limit} projects per account; delete some to make room.",
            details={"limit": limit},
        )


class ProjectFileNotFoundError(AppError):
    """No live file with that id in the project — the project itself has
    already been resolved as the caller's."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "project_file_not_found"


class ProjectFileLimitError(AppError):
    """The project already holds the most files it may. Counted by name: a
    new version of a file the project has is never refused."""

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "project_file_limit"

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"At most {limit} files per project; delete some to make room.",
            details={"limit": limit},
        )


# ── Skills ──────────────────────────────────────────────────────────────────


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


# ── Categories ──────────────────────────────────────────────────────────────


class CategoryNotFoundError(AppError):
    """No category with that id (admin-console lookup, both taxonomies).

    One error for ``skill_category`` and ``connector_category`` alike: they
    are separate tables, so passing one's id where the other is expected
    already lands here, and the operator's next move — pick a real id out of
    ``GET /{skill,connector}-categories`` — is the same either way. The
    message names the taxonomy; the type does not need to."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "category_not_found"


# ── Connectors ──────────────────────────────────────────────────────────────


class InvalidConnectorUrlError(AppError):
    """The Connector URL isn't a usable public https endpoint — wrong scheme,
    unresolvable host, or it resolves to a private / loopback / link-local
    address (the SSRF guard). 400: the client must supply a different URL."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "invalid_connector_url"


class ConnectorUnreachableError(AppError):
    """Couldn't complete the MCP handshake with the remote server. 502 — reads
    as an upstream failure the user can retry after fixing the server.

    The catch-all of a small family: the subclasses below name the failures a
    user would *do something different* about, and this one keeps the rest
    (timed out, DNS, connection refused). Splitting by remedy rather than by
    cause is deliberate — every one of these used to arrive as this single
    type, so the UI had one sentence for all of them and a report of "it says
    it cannot connect" was unactionable.

    Subclasses, not siblings, so the several ``except
    ConnectorUnreachableError`` sites keep catching the whole family without
    being told about each new member.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "connector_unreachable"


class ConnectorTlsFailedError(ConnectorUnreachableError):
    """TLS would not verify. Its own type because the remedy is unlike every
    other connection failure: the address and the port are right, and what has
    to change is a certificate — typically one that is self-signed, or issued
    by an authority this host does not trust."""

    error_type = "connector_tls_failed"


class ConnectorAuthRejectedError(ConnectorUnreachableError):
    """The server answered 401/403. The one failure here the user fixes by
    editing a credential rather than an address, which is why the generic
    wording used to name both and help with neither."""

    error_type = "connector_auth_rejected"


class ConnectorHttpStatusError(ConnectorUnreachableError):
    """The server answered HTTP, but not successfully (404, 500, …).

    Distinct from being unreachable because something *is* listening and did
    reply — so the network and the port are fine, and the next move is the
    path or the server itself. The status is in ``message``, which reaches the
    log rather than the UI."""

    error_type = "connector_http_error"


class ConnectorNotMcpEndpointError(ConnectorUnreachableError):
    """The server answered, but not with MCP — usually the legacy ``/sse``
    path rather than the Streamable HTTP endpoint. Only raised before the
    session opens; after that the endpoint has proved itself and a parse
    failure is the server misbehaving mid-catalogue."""

    error_type = "connector_not_mcp_endpoint"


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


class ConnectorOAuthError(AppError):
    """An OAuth step failed — metadata discovery, dynamic client registration,
    or the authorization-code exchange. 502: reads as an upstream/provider
    failure the user can retry after fixing the server or their OAuth app."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "connector_oauth_failed"


class ConnectorNotFoundError(AppError):
    """No Connector with that id that the caller owns. One answer for both,
    so the existence of other users' Connectors is not leaked."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "connector_not_found"


class ConnectorNotOwnedError(AppError):
    """A Connector selection named an id the caller does not own.

    Refused rather than dropped: an unknown id is a client bug, and storing
    it would leave a foreign id bound to this user's session or schedule.
    400 rather than the 404 a single-Connector path gives, because the id is
    one of several in a body the client composed and naming the refusal is
    the useful answer."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "connector_not_owned"


class ConnectorCatalogEntryNotFoundError(AppError):
    """No such entry in the official Connector directory — the id is unknown or
    the entry has been unpublished. 404 rather than 403 for an unpublished one:
    an unlisted entry should read as absent, not as "exists but withheld"."""

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "connector_catalog_entry_not_found"


class ConnectorCatalogCredentialError(AppError):
    """A catalog entry's credential requirement was not met: a BEARER entry
    was added without the user's token, or a credential was sent to an entry
    that takes none. 400: the request does not fit the entry."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "connector_catalog_credential"


class ConnectorCatalogUrlTakenError(AppError):
    """The catalog already offers this URL. Two entries pointing at one
    server are the same Connector offered twice, whatever they are
    named, so ``url`` carries the table's unique constraint — this is a
    conflict with an existing row, not a malformed request."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "connector_catalog_url_taken"


# ── Schedules ───────────────────────────────────────────────────────────────


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


# ── Meetings ────────────────────────────────────────────────────────────────
#
# The workspace acts as the user at the Meeting service, so most of these
# name something Meeting did or said; the ones that do not are conflicts
# between two edits of the same record.


class MeetingServiceUnavailableError(AppError):
    """Meeting could not be reached, or answered with something that is not
    an answer. Retrying later is the whole remedy."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "meeting_service_unavailable"


class MeetingRateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "meeting_rate_limited"


class MeetingSessionExpiredError(AppError):
    """Meeting no longer honours the session behind this sign-in. The local
    session has been ended with it; the user signs in again."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "meeting_session_expired"


class MeetingRefusedError(AppError):
    """Meeting understood the request and declined it. ``details["code"]``
    carries its code, for whoever reads the logs."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "meeting_refused"


class MeetingRecordUnreadableError(AppError):
    """Meeting answered with a record whose shape cannot be read without
    guessing — a transcript that would lose segments, two recognitions for
    one meeting, a record for a different meeting. Refused rather than
    shown with parts missing."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "meeting_record_unreadable"


class MeetingNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "meeting_not_found"


class MeetingChangedError(AppError):
    """The record changed since the client read it — another edit, here or
    in Meeting's own app — or another edit of it is in flight. Reload, then
    edit again."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "meeting_changed"


class MeetingNotEditableError(AppError):
    """The meeting has no recognition yet, so there is no transcript or
    minutes record to write to."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "meeting_not_editable"


class TranscriptEditUnsafeError(AppError):
    """The transcript's shape does not allow changing one segment without
    disturbing others; nothing was saved."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "transcript_edit_unsafe"


class MeetingSaveUnconfirmedError(AppError):
    """Meeting accepted the write but reading the record back does not show
    it. The edit may or may not have landed; reload to see."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "meeting_save_unconfirmed"


class MeetingNoTranscriptError(AppError):
    """Nothing to write minutes from or answer questions about yet."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "meeting_no_transcript"


class MeetingGenerationFailedError(AppError):
    """Meeting or the model did not produce a usable answer — cut short,
    empty, or refused. ``details["reason"]`` says which."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "meeting_generation_failed"


class MeetingModelUnavailableError(AppError):
    """The system model is not configured for meetings, or did not answer."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "meeting_model_unavailable"


class MeetingSettingsInvalidError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "meeting_settings_invalid"


class MeetingTemplateNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "meeting_template_not_found"


class TemplateImportUnsupportedError(AppError):
    """Only ``.docx``, ``.md`` and ``.txt`` documents can be read as templates."""

    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    error_type = "template_import_unsupported"


class TemplateImportTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    error_type = "template_import_too_large"


class TemplateImportUnreadableError(AppError):
    """The document could not be read as a template — malformed, not UTF-8,
    no recognizable structure, or too complex to parse safely."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "template_import_unreadable"


class VocabularyInvalidError(AppError):
    """A vocabulary change that cannot be made as asked — a duplicate name,
    one spelling corrected two ways, too many categories."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_type = "vocabulary_invalid"


class VocabularyCategoryNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "vocabulary_category_not_found"


class RecordingMediaUnsupportedError(AppError):
    """Only audio and video files can be uploaded as meeting recordings."""

    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    error_type = "recording_media_unsupported"


class RecordingTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    error_type = "recording_too_large"


class RecordingWindowInvalidError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "recording_window_invalid"


class LibraryChangedError(AppError):
    """A template or vocabulary changed since the editor read it — another
    tab, another device. Reload, then edit again."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "library_changed"


class RecordingNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "recording_not_found"


class RecordingConflictError(AppError):
    """The request does not fit the recording's state: it is already
    closed, a window is missing or differs, or it cannot be retried."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "recording_conflict"
