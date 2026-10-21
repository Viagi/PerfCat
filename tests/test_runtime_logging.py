# -*- encoding: utf-8 -*-
"""Runtime logging behavior tests."""
import io
import logging

from perfcat.core.logging_config import (
    STARTUP_LOG_ATTR,
    ConsoleLogFilter,
    _set_console_level,
)


def _log_record(level, message, **extra):
    record = logging.LogRecord(
        name="tests.runtime_logging.record",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_console_logging_keeps_startup_logs_and_errors_only():
    logger = logging.getLogger("tests.runtime_logging.console")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)

    try:
        for existing_handler in logger.handlers[:]:
            logger.removeHandler(existing_handler)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        _set_console_level(logger)
        _set_console_level(logger)

        logger.info("regular info")
        logger.warning("regular warning")
        logger.info("startup info", extra={STARTUP_LOG_ATTR: True})
        logger.error("regular error")

        console_filter = handler.filters[0]
        console_filter.level = logging.WARNING
        assert console_filter.filter(_log_record(logging.WARNING, "plain warning"))
        assert not console_filter.filter(_log_record(logging.INFO, "plain info"))

        output = stream.getvalue()
        assert "regular info" not in output
        assert "regular warning" not in output
        assert "startup info" in output
        assert "regular error" in output
        assert sum(isinstance(f, ConsoleLogFilter) for f in handler.filters) == 1
    finally:
        logger.removeHandler(handler)
        handler.close()
        for original_handler in original_handlers:
            logger.addHandler(original_handler)
        logger.setLevel(original_level)
        logger.propagate = original_propagate
