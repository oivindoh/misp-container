"""Unit tests for environment variable handling."""

import os
from unittest.mock import patch

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.env import env


class TestEnv:
    """The env() helper reads env vars."""

    def test_returns_env_var(self):
        """Env var is returned when set."""
        with patch.dict(os.environ, {"MY_VAR": "from_env"}):
            assert env("MY_VAR") == "from_env"

    def test_missing_returns_empty(self):
        """Missing env var returns empty string."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NONEXISTENT", None)
            assert env("NONEXISTENT") == ""

    def test_explicit_default(self):
        """Missing env var with explicit default uses that default."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NONEXISTENT", None)
            assert env("NONEXISTENT", "fallback") == "fallback"

    def test_env_var_wins(self):
        """Explicit env var takes precedence."""
        with patch.dict(os.environ, {"DB_HOST": "custom-host"}):
            assert env("DB_HOST") == "custom-host"
