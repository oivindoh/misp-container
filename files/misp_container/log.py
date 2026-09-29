"""Logging setup for MISP container entrypoints.

LOG_FORMAT selects the line format: json (one JSON object per line, the
Kubernetes default) or text (the Compose default, coloured on a terminal).
"""

import json
import logging
import os
import sys
import time

TEXT_FORMAT = "%(asctime)s %(levelname)-5s [%(context)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ANSI colours for the level in text mode on a terminal
COLOURS = {"DEBUG": "\033[2m", "INFO": "\033[36m", "WARNING": "\033[33m",
           "ERROR": "\033[31m", "CRITICAL": "\033[1;31m"}
RESET = "\033[0m"


class ContextFilter(logging.Filter):
    """Inject a default context into log records that don't have one."""

    def __init__(self, default_context: str = "misp"):
        super().__init__()
        self.default_context = default_context

    def filter(self, record):
        if not hasattr(record, "context"):
            record.context = self.default_context
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time (UTC, RFC 3339), level, context, message."""

    def format(self, record):
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                    + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "context": getattr(record, "context", "misp"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """The text line, with the level coloured when colour is on."""

    def __init__(self, colour: bool = False):
        super().__init__(TEXT_FORMAT, datefmt=DATE_FORMAT)
        self.colour = colour

    def format(self, record):
        line = super().format(record)
        if self.colour and record.levelname in COLOURS:
            level = f"{record.levelname:<5}"
            line = line.replace(level, f"{COLOURS[record.levelname]}{level}{RESET}", 1)
        return line


def log_format() -> str:
    """json or text, from LOG_FORMAT; anything else is text."""
    return "json" if os.environ.get("LOG_FORMAT", "").strip().lower() == "json" else "text"


def formatter(stream=None) -> logging.Formatter:
    if log_format() == "json":
        return JsonFormatter()
    stream = stream or sys.stdout
    return TextFormatter(colour=hasattr(stream, "isatty") and stream.isatty())


def setup(context: str = "misp", level: int = logging.INFO) -> logging.Logger:
    """Configure and return the root logger for a container entrypoint."""
    logger = logging.getLogger("misp")
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter(sys.stdout))
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
