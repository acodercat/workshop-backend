"""Domain exceptions raised by session-service code.

Service-layer helpers shouldn't know they're being called from HTTP —
that's a Dependency-Inversion violation that couples them to FastAPI.
The endpoint layer translates these into HTTP status codes.
"""


class SessionServiceError(Exception):
    """Base for all session-service domain errors."""


class GenerationInProgressError(SessionServiceError):
    """A new turn was requested while one is still running."""


class ConcurrentTaskLimitError(SessionServiceError):
    """The user already has the max number of simultaneously RUNNING tasks.

    A per-user resource cap (``settings.max_concurrent_running_tasks``)
    checked at enqueue under the reservation's user-row lock, so two racing
    enqueues for the same user can't both slip past. The endpoint turns it
    into 429 (Too Many Requests)."""


class SessionGoneError(SessionServiceError):
    """The session was soft-deleted (or hard-deleted) out from under an
    in-flight enqueue.

    ``create_message`` checks ownership/liveness via ``_get_or_404`` on an
    unlocked read, then ``queue_user_message`` re-locks the row; a delete
    that commits in that window is caught here under the lock so we never
    enqueue a turn against a tombstoned session. The endpoint turns it
    into 404, matching how every other read treats a deleted session."""


class RecipeNotInvokableError(SessionServiceError):
    """A ``recipe_id`` was sent that the caller can't invoke.

    The recipe doesn't exist, isn't visible to this user (a CUSTOM
    recipe they don't own), or is disabled. Unlike skills — silently
    filtered so a stale toggle doesn't block a send — a recipe is an
    explicit single pick, so the endpoint turns this into 400 rather
    than dropping it silently."""


class UnconfiguredTierError(SessionServiceError):
    """A capability tier resolved to no usable ``model_id``.

    Either ``settings.session_tier_models`` has no entry for it, or the
    entry names a model missing from ``settings.supported_session_model_ids``
    — a deployment-configuration fault, not a caller mistake, but it can
    only be detected when someone asks for that tier. The endpoint turns it
    into 400; the scheduled-task dispatcher logs it and skips the
    occurrence, since no retry will fix a config file."""
