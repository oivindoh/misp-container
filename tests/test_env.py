"""Unit tests for environment variable handling."""

import os
from unittest.mock import patch

import pytest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.env import env, apply_defaults


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
        with patch.dict(os.environ, {"MYSQL_HOST": "custom-host"}):
            assert env("MYSQL_HOST") == "custom-host"


class TestApplyDefaults:
    """apply_defaults() loads settings.yaml defaults into os.environ."""

    def test_worker_fallback(self):
        """WORKERS env var sets default for all queue worker counts."""
        with patch.dict(os.environ, {"WORKERS": "2"}, clear=False):
            for q in ("DEFAULT", "PRIO", "EMAIL", "CACHE"):
                os.environ.pop(f"NUM_WORKERS_{q}", None)
            os.environ.pop("NUM_WORKERS_UPDATE", None)
            apply_defaults()
            assert os.environ["NUM_WORKERS_DEFAULT"] == "2"
            assert os.environ["NUM_WORKERS_PRIO"] == "2"
            assert os.environ["NUM_WORKERS_UPDATE"] == "1"

    def test_worker_explicit_override(self):
        """Explicit NUM_WORKERS_* env vars are preserved."""
        with patch.dict(os.environ, {"NUM_WORKERS_DEFAULT": "3"}, clear=False):
            apply_defaults()
            assert os.environ["NUM_WORKERS_DEFAULT"] == "3"
