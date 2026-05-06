"""Domain exceptions raised by session-service code.

Service-layer helpers shouldn't know they're being called from HTTP —
that's a Dependency-Inversion violation that couples them to FastAPI.
The endpoint layer translates these into HTTP status codes.
"""


class SessionServiceError(Exception):
    """Base for all session-service domain errors."""


class GenerationInProgressError(SessionServiceError):
    """A new turn was requested while one is still running."""


class FileNotInSessionError(SessionServiceError):
    """A file_key was looked up against a session it doesn't belong to.

    Surfaces both the owner-side (caller's own session) and share-side
    (recipient's accessible share) authorization miss in one shape; the
    endpoint layer turns it into 403."""
