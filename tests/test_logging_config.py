"""A logged traceback names the frames it passed through, never their variables."""

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from app.logging_config import setup_logging

_SECRET = "hunter2-not-for-any-log"


def _loggers() -> Iterator[tuple[str, logging.Logger]]:
    yield "", logging.getLogger()
    for name, candidate in logging.Logger.manager.loggerDict.items():
        if isinstance(candidate, logging.Logger):
            yield name, candidate


@pytest.fixture
def logging_restored() -> Iterator[None]:
    """``setup_logging`` reconfigures logging for the whole process; every logger
    is put back as it was, and one it created is left as a fresh one would be,
    so no later test inherits the configuration."""
    saved = {name: (log.handlers[:], log.level, log.propagate) for name, log in _loggers()}
    yield
    logger.remove()
    logger.add(sys.stderr)
    for name, log in _loggers():
        handlers, level, propagate = saved.get(name, ([], logging.NOTSET, True))
        log.handlers[:] = handlers
        log.setLevel(level)
        log.propagate = propagate


def _fail_holding(password: str) -> None:
    raise ValueError("refused")


def test_a_logged_traceback_leaves_out_frame_variables(tmp_path: Path, logging_restored):
    setup_logging(log_dir=str(tmp_path))
    try:
        _fail_holding(_SECRET)
    except ValueError:
        logging.getLogger(__name__).exception("request failed")
    logger.complete()
    logged = {path.name: path.read_text() for path in tmp_path.iterdir()}

    assert "_fail_holding" in logged["error.log"]
    assert all(_SECRET not in text for text in logged.values())
