from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException


async def integrity_error_handler(request: Request, exc: IntegrityError):
    logger.warning(f"Integrity error: {exc}")
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": "Conflict",
            "message": "The request violates a database constraint.",
        },
    )


async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    logger.error(f"Database error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Database Error",
            "message": "An error occurred while processing your request.",
        },
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "HTTP Exception", "message": str(exc.detail)},
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Pydantic / FastAPI request-body validation failure.

    The project's standard envelope is ``{error, message}`` with
    ``message`` *always* a string — frontend's ``extractMessage``
    pulls it straight into toasts / banners without parsing
    structured data. The structured per-field breakdown (which
    fields failed, what kind of failure) moves to an optional
    ``details`` slot so callers that *do* want machine-readable
    output can opt in.
    """
    errors = [
        {"location": list(err["loc"]), "message": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": "Validation Error",
            "message": "Request body failed validation.",
            "details": {"errors": errors},
        },
    )


async def general_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unexpected error: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal Server Error",
            "message": "An unexpected error occurred.",
        },
    )
