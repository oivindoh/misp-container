"""Unit tests for the container log formats (LOG_FORMAT json or text)."""

import io
import json
import logging
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import log as mlog


def _record(msg="hello %s", args=("world",), level=logging.WARNING, context="worker", exc_info=None):
    record = logging.LogRecord("misp", level, __file__, 1, msg, args, exc_info)
    record.context = context
    return record


class TestJsonFormatter:
    def test_one_object_with_the_four_fields(self):
        line = mlog.JsonFormatter().format(_record())
        entry = json.loads(line)
        assert list(entry) == ["time", "level", "context", "message"]
        assert entry["level"] == "warning" and entry["context"] == "worker"
        assert entry["message"] == "hello world"
        assert entry["time"].endswith("Z") and "T" in entry["time"]

    def test_compact_so_level_and_context_grep_together(self):
        assert '"level":"warning","context":"worker"' in mlog.JsonFormatter().format(_record())

    def test_exception_in_its_own_field(self):
        try:
            raise ValueError("boom")
        except ValueError:
            record = _record(exc_info=sys.exc_info())
        entry = json.loads(mlog.JsonFormatter().format(record))
        assert "ValueError: boom" in entry["exception"]
        assert "\n" not in mlog.JsonFormatter().format(record)


class TestTextFormatter:
    def test_plain_line(self):
        line = mlog.TextFormatter(colour=False).format(_record())
        assert line.endswith("WARNING [worker] hello world")
        assert "\033[" not in line

    def test_colour_marks_the_level(self):
        line = mlog.TextFormatter(colour=True).format(_record())
        assert "\033[33mWARNING\033[0m" in line


class TestFormatSelection:
    def test_json_from_env(self):
        with patch.dict(os.environ, {"LOG_FORMAT": " JSON "}):
            assert mlog.log_format() == "json"
            assert isinstance(mlog.formatter(io.StringIO()), mlog.JsonFormatter)

    def test_text_is_the_default(self):
        with patch.dict(os.environ, {"LOG_FORMAT": ""}):
            assert mlog.log_format() == "text"
        with patch.dict(os.environ, {"LOG_FORMAT": "yaml"}):
            assert mlog.log_format() == "text"

    def test_no_colour_without_a_terminal(self):
        with patch.dict(os.environ, {"LOG_FORMAT": "text"}):
            fmt = mlog.formatter(io.StringIO())
        assert isinstance(fmt, mlog.TextFormatter) and not fmt.colour
