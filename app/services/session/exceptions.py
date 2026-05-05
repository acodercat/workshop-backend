"""Domain exceptions raised by session-service code.

Service-layer helpers shouldn't know they're being called from HTTP —
that's a Dependency-Inversion violation that couples them to FastAPI.
The endpoint layer translates these into HTTP status codes.
"""


class SessionServiceError(Exception):
    """Base for all session-service domain errors."""


class GenerationInProgressError(SessionServiceError):
    """A new turn was requested while one is still running."""
