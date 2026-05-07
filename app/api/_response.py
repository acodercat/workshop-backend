"""Shared response helpers for API endpoints.

Used by ``auth`` and ``user`` endpoint modules to emit the project's
standard ``{error, message}`` envelope with a *specific* ``error``
reason code (e.g. ``"username_taken"``, ``"invalid_code"``) instead
of the generic ``"HTTP Exception"`` the global
:func:`http_exception_handler` would emit.

Returning :class:`JSONResponse` directly bypasses the global handler
— it's the right escape hatch when the endpoint wants a machine-
readable reason the frontend can branch on without string-matching
the ``message`` field.
"""

from fastapi.responses import JSONResponse


def error_response(
    *,
    status_code: int,
    error: str,
    message: str,
) -> JSONResponse:
    """Build a ``{error, message}`` envelope with a structured reason
    code.

    ``error`` is a short, stable identifier (snake_case) the frontend
    branches on; ``message`` is the human-readable text shown to the
    user when there's no localized override available.
    """
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "message": message},
    )


__all__ = ["error_response"]
