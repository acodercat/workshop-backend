"""Logging configuration using Loguru.

App code uses stdlib ``logging`` (``logging.getLogger(__name__)``);
the loguru-managed sinks (rotating files, colored stderr) ingest
those records via :class:`InterceptHandler` configured below.
"""
import sys
import logging
from pathlib import Path

from loguru import logger


class InterceptHandler(logging.Handler):
    """Forward stdlib ``logging`` records into loguru sinks."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Walk back through stdlib ``logging`` frames so loguru's
        # ``{name}`` placeholder resolves to the actual caller's
        # module instead of stdlib internals (``callHandlers`` etc.).
        # Starting depth 6 matches loguru's documented example —
        # covers the ``emit`` → ``Handler.handle`` →
        # ``Logger.callHandlers`` → ``Logger.handle`` →
        # ``Logger._log`` → ``Logger.<level>`` chain.
        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(log_dir: str = "logs", level: str = "INFO"):
    """
    Setup Loguru with local file logging.

    Args:
        log_dir: Directory to store log files
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    # Create logs directory
    Path(log_dir).mkdir(exist_ok=True)

    # Remove default handler
    logger.remove()

    # Default ``request_id`` so the ``{extra[request_id]}`` field below
    # renders for logs emitted outside a request (startup, shutdown,
    # background tasks). The request-id middleware overrides it per
    # request via ``logger.contextualize``.
    logger.configure(extra={"request_id": "-"})

    # Intercept standard logging
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.DEBUG, force=True)

    # Also intercept uvicorn and other libraries
    for name in ["uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy", "httpx"]:
        logging.getLogger(name).handlers = [InterceptHandler()]

    # Console output (colored)
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <magenta>{extra[request_id]}</magenta> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )

    # Main log file (with rotation)
    logger.add(
        f"{log_dir}/app.log",
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[request_id]} | {name}:{function}:{line} - {message}",
        rotation="10 MB",
        retention="7 days",
        compression="gz",
    )

    # Separate error log file
    logger.add(
        f"{log_dir}/error.log",
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[request_id]} | {name}:{function}:{line} - {message}",
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        backtrace=True,
        diagnose=True,
    )

    return logger
