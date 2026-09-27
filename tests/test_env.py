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


class TestDerivedDefaults:
    """Secondary settings inherit from the primary env var unless set."""

    def test_redis_mirrors_inherit(self):
        env = {"MISP_REDIS_HOST": "cache", "MISP_REDIS_PORT": "6380", "MISP_REDIS_PASSWORD": "pw"}
        with patch.dict(os.environ, env, clear=False):
            for key in ("SIMPLEBACKGROUNDJOBS_REDIS_HOST", "SIMPLEBACKGROUNDJOBS_REDIS_PASSWORD",
                        "PLUGIN_ZEROMQ_REDIS_PORT"):
                os.environ.pop(key, None)
            apply_defaults()
            assert os.environ["SIMPLEBACKGROUNDJOBS_REDIS_HOST"] == "cache"
            assert os.environ["SIMPLEBACKGROUNDJOBS_REDIS_PASSWORD"] == "pw"
            assert os.environ["PLUGIN_ZEROMQ_REDIS_PORT"] == "6380"

    def test_explicit_value_wins(self):
        env = {"MISP_REDIS_HOST": "cache", "SIMPLEBACKGROUNDJOBS_REDIS_HOST": "jobs-cache"}
        with patch.dict(os.environ, env, clear=False):
            apply_defaults()
            assert os.environ["SIMPLEBACKGROUNDJOBS_REDIS_HOST"] == "jobs-cache"

    def test_email_and_baseurl_inherit(self):
        env = {"MISP_BASEURL": "https://m", "ADMIN_EMAIL": "a@x"}
        with patch.dict(os.environ, env, clear=False):
            for key in ("MISP_EXTERNAL_BASEURL", "MISP_CONTACT", "GNUPG_EMAIL", "MISP_EMAIL"):
                os.environ.pop(key, None)
            apply_defaults()
            assert os.environ["MISP_EXTERNAL_BASEURL"] == "https://m"
            assert os.environ["MISP_CONTACT"] == "a@x"
            assert os.environ["GNUPG_EMAIL"] == "a@x"


class TestAuthAliases:
    def test_documented_oidc_names_feed_derived_ones(self):
        env = {"OIDC_PROVIDER_URL": "https://idp", "OIDC_ROLES_MAPPING": '{"a": 1}', "OIDC_MIXEDAUTH": "true"}
        with patch.dict(os.environ, env, clear=False):
            for key in ("OIDCAUTH_PROVIDER_URL", "OIDCAUTH_ROLE_MAPPER", "OIDCAUTH_MIXEDAUTH"):
                os.environ.pop(key, None)
            apply_defaults()
            assert os.environ["OIDCAUTH_PROVIDER_URL"] == "https://idp"
            assert os.environ["OIDCAUTH_ROLE_MAPPER"] == '{"a": 1}'
            assert os.environ["OIDCAUTH_MIXEDAUTH"] == "true"

    def test_derived_name_wins_over_alias(self):
        with patch.dict(os.environ, {"OIDC_CLIENT_ID": "short", "OIDCAUTH_CLIENT_ID": "derived"}):
            apply_defaults()
            assert os.environ["OIDCAUTH_CLIENT_ID"] == "derived"
