"""Unit tests for the log relay (misp_container.logrelay)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.logrelay import Relay


def _relay(tmp_path, max_bytes=1000):
    seen = []
    relay = Relay(directory=tmp_path, files=("server-sync.log",), max_bytes=max_bytes,
                  emit=lambda name, line: seen.append((name, line)))
    return relay, seen


class TestRelay:
    def test_new_lines_once(self, tmp_path):
        relay, seen = _relay(tmp_path)
        log = tmp_path / "server-sync.log"
        log.write_text("one\ntwo\n")
        assert relay.poll() == 2
        with open(log, "a") as f:
            f.write("three\n")
        relay.poll()
        assert [line for _, line in seen] == ["one", "two", "three"]

    def test_partial_line_waits_for_its_end(self, tmp_path):
        relay, seen = _relay(tmp_path)
        log = tmp_path / "server-sync.log"
        log.write_text("half")
        assert relay.poll() == 0
        with open(log, "a") as f:
            f.write(" done\n")
        relay.poll()
        assert seen == [("server-sync.log", "half done")]

    def test_file_over_the_limit_is_emptied(self, tmp_path):
        relay, seen = _relay(tmp_path, max_bytes=10)
        log = tmp_path / "server-sync.log"
        log.write_text("a long line over ten bytes\n")
        relay.poll()
        assert log.stat().st_size == 0
        with open(log, "a") as f:
            f.write("after\n")
        relay.poll()
        assert [line for _, line in seen] == ["a long line over ten bytes", "after"]

    def test_file_emptied_elsewhere_starts_over(self, tmp_path):
        relay, seen = _relay(tmp_path)
        log = tmp_path / "server-sync.log"
        log.write_text("first line\n")
        relay.poll()
        log.write_text("new\n")
        relay.poll()
        assert [line for _, line in seen] == ["first line", "new"]

    def test_missing_file_is_skipped(self, tmp_path):
        relay, seen = _relay(tmp_path)
        assert relay.poll() == 0 and seen == []

    def test_default_emit_uses_the_file_as_context(self, tmp_path, capsys):
        from unittest.mock import patch
        with patch("misp_container.logrelay.getlog") as getlog:
            Relay._log("server-sync.log", "GET /x 200")
        getlog.assert_called_once_with("misp:server-sync")
