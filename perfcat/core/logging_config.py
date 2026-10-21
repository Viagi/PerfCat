# -*- encoding: utf-8 -*-
"""Runtime logging configuration for PerfCat."""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_FILE = "perfcat.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s:%(lineno)d %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_LOG_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
CONSOLE_LOG_LEVEL = logging.ERROR
STARTUP_LOG_LEVEL = logging.INFO
STARTUP_LOG_ATTR = "perfcat_startup"


class ConsoleLogFilter(logging.Filter):
    """Allow errors and explicitly-marked startup logs on the console."""

    def __init__(self, level=CONSOLE_LOG_LEVEL):
        super().__init__()
        self.level = level

    def filter(self, record):
        if record.levelno >= self.level:
            return True
        return bool(getattr(record, STARTUP_LOG_ATTR, False))


class TeeStdout:
    """Write stdout to a log file, optionally echoing to the original stream."""

    def __init__(self, stream, log_path, echo=False):
        self.stream = stream
        self.log_path = log_path
        self.echo = echo
        self._file = open(log_path, "a", encoding="utf-8", buffering=1)

    def write(self, data):
        if self.echo:
            self.stream.write(data)
        self._file.write(data)

    def flush(self):
        if self.echo:
            self.stream.flush()
        self._file.flush()

    def isatty(self):
        return getattr(self.stream, "isatty", lambda: False)()

    def fileno(self):
        return self.stream.fileno()

    @property
    def encoding(self):
        return getattr(self.stream, "encoding", "utf-8")


def default_log_path():
    return os.path.join(os.getcwd(), DEFAULT_LOG_DIR, DEFAULT_LOG_FILE)


def _ensure_log_dir(log_path):
    log_dir = os.path.dirname(os.path.abspath(log_path))
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)


def _has_file_handler(logger, log_path):
    abs_path = os.path.abspath(log_path)
    for handler in logger.handlers:
        if getattr(handler, "_perfcat_log_path", None) == abs_path:
            return True
    return False


def _add_file_handler(logger, log_path, level=logging.INFO):
    if _has_file_handler(logger, log_path):
        return

    handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    handler._perfcat_log_path = os.path.abspath(log_path)
    logger.addHandler(handler)
    logger.setLevel(min(logger.level or level, level))


def _set_console_level(logger, level=CONSOLE_LOG_LEVEL):
    has_console_handler = False
    for handler in logger.handlers:
        if getattr(handler, "_perfcat_log_path", None):
            continue
        if isinstance(handler, logging.StreamHandler):
            handler.setLevel(min(level, STARTUP_LOG_LEVEL))
            console_filter = getattr(handler, "_perfcat_console_filter", None)
            if isinstance(console_filter, ConsoleLogFilter):
                console_filter.level = level
            else:
                console_filter = ConsoleLogFilter(level)
                handler.addFilter(console_filter)
                handler._perfcat_console_filter = console_filter
            has_console_handler = True

    if not has_console_handler and logger is logging.getLogger():
        handler = logging.StreamHandler()
        handler.setLevel(min(level, STARTUP_LOG_LEVEL))
        console_filter = ConsoleLogFilter(level)
        handler.addFilter(console_filter)
        handler._perfcat_console_filter = console_filter
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        logger.addHandler(handler)


def _tee_stdout(log_path, echo=False):
    if getattr(sys.stdout, "_perfcat_log_path", None) == os.path.abspath(log_path):
        return
    tee = TeeStdout(sys.stdout, log_path, echo=echo)
    tee._perfcat_log_path = os.path.abspath(log_path)
    sys.stdout = tee


def setup_runtime_logging(log_file=None, tee_stdout=True, stdout_to_console=False):
    """Configure PerfCat runtime logs.

    Args:
        log_file: Output file path. Defaults to ``./logs/perfcat.log``.
        tee_stdout: Copy ``print`` output to the same log file.
        stdout_to_console: Echo captured stdout back to the console.

    Returns:
        str: Absolute path of the configured log file.
    """
    log_path = os.path.abspath(log_file or default_log_path())
    _ensure_log_dir(log_path)

    root_logger = logging.getLogger()
    _add_file_handler(root_logger, log_path)
    _set_console_level(root_logger)
    _set_console_level(logging.getLogger("werkzeug"))
    _set_console_level(logging.getLogger("flask.app"))

    try:
        from logzero import logger as logzero_logger

        _add_file_handler(logzero_logger, log_path)
        _set_console_level(logzero_logger)
    except Exception:
        pass

    if tee_stdout:
        _tee_stdout(log_path, echo=stdout_to_console)

    return log_path
