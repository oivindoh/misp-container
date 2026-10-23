"""Unit tests for the config diff engine."""

import json
import os
from unittest.mock import patch

import pytest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.config import (
    SettingsCache,
    SettingSpec,
    _read_file,
    load_settings_yaml,
    derive_env_var,
)


class TestSettingsCache:
    """In-memory cache for MISP settings loaded from DB + config.php."""

    def test_normalise_quoted_string(self):
        """DB stores strings as '"value"' -- normalise strips the quotes."""
        cache = SettingsCache()
        assert cache.normalise('"hello"') == "hello"

    def test_normalise_unquoted(self):
        """Booleans and numbers are stored unquoted -- normalise passes through."""
        cache = SettingsCache()
        assert cache.normalise("true") == "true"

    def test_normalise_number(self):
        """Numeric strings pass through unchanged."""
        cache = SettingsCache()
        assert cache.normalise("42") == "42"

    def test_normalise_none(self):
        """None input returns None (setting not found)."""
        cache = SettingsCache()
        assert cache.normalise(None) is None

    def test_has_and_get(self):
        """Basic key presence check and value retrieval."""
        cache = SettingsCache()
        cache.settings = {"MISP.baseurl": '"https://example.com"'}
        assert cache.has("MISP.baseurl") is True
        assert cache.has("MISP.nonexistent") is False
        assert cache.get("MISP.baseurl") == '"https://example.com"'
        assert cache.get("MISP.nonexistent") is None

class TestSettingSpec:
    """SettingSpec dataclass behaviour."""

    def test_from_dict(self):
        """Create a SettingSpec from a YAML-style dict."""
        spec = SettingSpec.from_dict("MISP.baseurl", {"value": "https://x.com", "force": True})
        assert spec.name == "MISP.baseurl"
        assert spec.default_value == "https://x.com"
        assert spec.force is True
        assert spec.blank_protection is False

    def test_env_var_derivation(self):
        """Env var name is auto-derived from setting name."""
        spec = SettingSpec(name="Plugin.S3_bucket_name", default_value="")
        assert spec.env_var == "PLUGIN_S3_BUCKET_NAME"

    def test_env_override(self):
        """Setting detects env var override."""
        spec = SettingSpec(name="MISP.baseurl", default_value="https://default.com")
        with patch.dict(os.environ, {"MISP_BASEURL": "https://overridden.com"}):
            assert spec.is_envar is True
            assert spec.effective_value == "https://overridden.com"

    def test_no_env_uses_default(self):
        """Without env var, uses default value."""
        spec = SettingSpec(name="MISP.language", default_value="eng")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MISP_LANGUAGE", None)
            assert spec.is_envar is False
            assert spec.effective_value == "eng"

    def test_is_sensitive(self):
        """Sensitive settings are marked via the sensitive field."""
        assert SettingSpec(name="Security.salt", default_value="x", sensitive=True).print_value == "<REDACTED>"
        assert SettingSpec(name="MISP.baseurl", default_value="x").print_value == "x"

    def test_print_value_redacts_sensitive(self):
        """Sensitive values are redacted in print_value."""
        spec = SettingSpec(name="Security.salt", default_value="mysecretvalue", sensitive=True)
        assert spec.print_value == "m<REDACTED>e"

    def test_print_value_shows_nonsensitive(self):
        """Non-sensitive values are shown in full."""
        spec = SettingSpec(name="MISP.baseurl", default_value="https://example.com")
        assert spec.print_value == "https://example.com"


class TestLoadSettingsYaml:
    """Load the new grouped settings.yaml format into SettingSpecs."""

    def test_loads_groups(self, tmp_path):
        """Settings are grouped by group level."""
        f = tmp_path / "settings.yaml"
        f.write_text("""
settings:
  initialisation:
    MISP.baseurl:
      value: https://localhost
    MISP.language:
      value: eng
  minimum_config:
    MISP.redis_host:
      value: redis
""")
        groups = load_settings_yaml(str(f))
        assert "initialisation" in groups
        assert "minimum_config" in groups
        assert len(groups["initialisation"]) == 2
        assert len(groups["minimum_config"]) == 1

    def test_env_var_override(self, tmp_path):
        """Setting with matching env var is detected as envar."""
        f = tmp_path / "settings.yaml"
        f.write_text("""
settings:
  test:
    MISP.baseurl:
      value: https://default.com
""")
        with patch.dict(os.environ, {"MISP_BASEURL": "https://overridden.com"}):
            groups = load_settings_yaml(str(f))
            spec = groups["test"][0]
            assert spec.is_envar is True
            assert spec.effective_value == "https://overridden.com"
            assert spec.default_value == "https://default.com"

    def test_no_env_var_uses_default(self, tmp_path):
        """Setting without matching env var uses default and is not envar."""
        f = tmp_path / "settings.yaml"
        f.write_text("""
settings:
  test:
    MISP.tmpdir:
      value: /var/www/MISP/app/tmp
""")
        # Ensure the auto-derived env var is not set
        env_key = "MISP_TMPDIR"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(env_key, None)
            groups = load_settings_yaml(str(f))
        assert groups["test"][0].default_value == "/var/www/MISP/app/tmp"
        assert groups["test"][0].is_envar is False

    def test_preserves_metadata(self, tmp_path):
        """force and blank_protection are preserved."""
        f = tmp_path / "settings.yaml"
        f.write_text("""
settings:
  test:
    Security.salt:
      value: ""
      force: true
      blank_protection: true
""")
        groups = load_settings_yaml(str(f))
        spec = groups["test"][0]
        assert spec.force is True
        assert spec.blank_protection is True

    def test_boolean_values(self, tmp_path):
        """YAML booleans (true/false) are converted to strings."""
        f = tmp_path / "settings.yaml"
        f.write_text("""
settings:
  test:
    MISP.background_jobs:
      value: true
    Plugin.ZeroMQ_enable:
      value: false
""")
        groups = load_settings_yaml(str(f))
        vals = {s.name: s.default_value for s in groups["test"]}
        assert vals["MISP.background_jobs"] == "true"
        assert vals["Plugin.ZeroMQ_enable"] == "false"

    def test_same_setting_in_multiple_groups(self, tmp_path):
        """Same setting can appear in multiple groups (e.g., Security.salt)."""
        f = tmp_path / "settings.yaml"
        f.write_text("""
settings:
  minimum_config:
    Security.salt:
      value: ""
      force: true
  initialisation:
    Security.salt:
      value: ""
      force: true
      blank_protection: true
""")
        groups = load_settings_yaml(str(f))
        assert len(groups["minimum_config"]) == 1
        assert len(groups["initialisation"]) == 1
        assert groups["minimum_config"][0].name == "Security.salt"
        assert groups["initialisation"][0].name == "Security.salt"
        assert groups["initialisation"][0].blank_protection is True

    def test_derive_env_var(self):
        """Setting names are converted to env var names correctly."""
        from misp_container.config import derive_env_var
        assert derive_env_var("MISP.redis_host") == "MISP_REDIS_HOST"
        assert derive_env_var("Plugin.S3_bucket_name") == "PLUGIN_S3_BUCKET_NAME"
        assert derive_env_var("Security.salt") == "SECURITY_SALT"
        assert derive_env_var("debug") == "DEBUG"


class TestReadFile:
    """Safe file reading with fallback defaults."""

    def test_reads_file(self, tmp_path):
        """Returns stripped file contents."""
        f = tmp_path / "version"
        f.write_text("v2.5.37\n")
        assert _read_file(str(f)) == "v2.5.37"

    def test_missing_file_returns_default(self):
        """Missing file returns the explicit default."""
        assert _read_file("/nonexistent/path", "fallback") == "fallback"

    def test_missing_file_returns_empty_by_default(self):
        """Missing file with no default returns empty string."""
        assert _read_file("/nonexistent/path") == ""


class TestTrackOnly:
    """Catalogue entries are never applied as defaults but honour env overrides."""

    def test_default_not_applied(self):
        from unittest.mock import patch as _patch
        spec = SettingSpec(name="MISP.catalogued", default_value="x", track_only=True)
        cache = SettingsCache()
        with _patch("misp_container.config.cake.set_setting") as set_setting:
            cache.apply_defaults([spec], "upstream")
        set_setting.assert_not_called()

    def test_env_override_enforced(self):
        from unittest.mock import patch as _patch
        spec = SettingSpec(name="MISP.catalogued", default_value="x", track_only=True)
        cache = SettingsCache()
        with patch.dict(os.environ, {"MISP_CATALOGUED": "y"}), \
                _patch("misp_container.config.cake.set_setting") as set_setting:
            cache.enforce_envars([spec], "upstream")
        set_setting.assert_called_once_with("MISP.catalogued", "y", force=False)


class TestBlankProtectedDefault:
    def test_blank_default_not_applied(self):
        from unittest.mock import patch as _patch
        spec = SettingSpec(name="SMIME.password", default_value="", blank_protection=True)
        cache = SettingsCache()
        with _patch("misp_container.config.cake.set_setting") as set_setting:
            cache.apply_defaults([spec], "gpg")
        set_setting.assert_not_called()

    def test_non_blank_default_applied(self):
        from unittest.mock import patch as _patch
        spec = SettingSpec(name="SMIME.password", default_value="x", blank_protection=True)
        cache = SettingsCache()
        with _patch("misp_container.config.cake.set_setting") as set_setting:
            cache.apply_defaults([spec], "gpg")
        set_setting.assert_called_once()


def test_no_database_default_is_an_empty_string_without_blank_protection():
    """MISP stores no empty setting, so the configure step would write it again on every run."""
    from misp_container import configure
    path = os.path.join(os.path.dirname(__file__), "..", "files", "misp-config", "settings.yaml")
    groups = load_settings_yaml(path)
    empty = [spec.name for group in configure.DB_GROUPS if group != "upstream" for spec in groups.get(group, [])
             if spec.default_value == "" and not spec.blank_protection and not spec.track_only]
    assert empty == []
