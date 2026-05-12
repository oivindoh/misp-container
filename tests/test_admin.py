"""Unit tests for admin helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.admin import _sql_escape


class TestSqlEscape:
    """SQL string escaping to prevent injection."""

    def test_clean_string(self):
        """Normal strings pass through unchanged."""
        assert _sql_escape("admin@example.com") == "admin@example.com"

    def test_single_quote(self):
        """Single quotes are escaped."""
        assert _sql_escape("it's") == "it\\'s"

    def test_backslash(self):
        """Backslashes are escaped."""
        assert _sql_escape("path\\to") == "path\\\\to"

    def test_newline(self):
        """Newlines are escaped."""
        assert _sql_escape("line1\nline2") == "line1\\nline2"

    def test_combined(self):
        """Multiple special chars in one string."""
        assert _sql_escape("it's a \\test\n") == "it\\'s a \\\\test\\n"

    def test_empty(self):
        """Empty string returns empty."""
        assert _sql_escape("") == ""

    def test_uuid(self):
        """UUIDs pass through unchanged."""
        assert _sql_escape("550e8400-e29b-41d4-a716-446655440000") == "550e8400-e29b-41d4-a716-446655440000"
