"""Logging setup for MISP container entrypoints."""

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(context)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class ContextFilter(logging.Filter):
    """Inject a default context into log records that don't have one."""

    def __init__(self, default_context: str = "misp"):
        super().__init__()
        self.default_context = default_context

    def filter(self, record):
        if not hasattr(record, "context"):
            record.context = self.default_context
        return True


def setup(context: str = "misp", level: int = logging.INFO) -> logging.Logger:
    """Configure and return the root logger for a container entrypoint."""
    logger = logging.getLogger("misp")
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler.addFilter(ContextFilter(context))

    logger.addHandler(handler)
    logger.setLevel(level)
    return logger


def get(context: str | None = None) -> logging.LoggerAdapter:
    """Get a logger adapter with a specific context label."""
    logger = logging.getLogger("misp")
    if not logger.handlers:
        setup()
    extra = {"context": context} if context else {}
    return logging.LoggerAdapter(logger, extra)
