"""Logging configuration using Loguru."""
import sys
import logging
from pathlib import Path
from loguru import logger


class InterceptHandler(logging.Handler):
    """Intercept standard logging and redirect to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


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

    # Intercept standard logging
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.DEBUG, force=True)

    # Also intercept uvicorn and other libraries
    for name in ["uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy", "httpx"]:
        logging.getLogger(name).handlers = [InterceptHandler()]

    # Console output (colored)
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )

    # Main log file (with rotation)
    logger.add(
        f"{log_dir}/app.log",
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="10 MB",
        retention="7 days",
        compression="gz",
    )

    # Separate error log file
    logger.add(
        f"{log_dir}/error.log",
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        backtrace=True,
        diagnose=True,
    )

    return logger
