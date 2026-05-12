"""MISP settings diff engine.

Loads settings from DB + config.php, compares against desired values from
settings.yaml, and only calls cake for settings that actually changed.

Env var convention:
    Any setting can be overridden by an env var derived from the setting name:
      MISP.redis_host -> MISP_REDIS_HOST
    If the env var exists and is non-empty, the setting is enforced every startup.
    If no env var, the default from settings.yaml is applied once.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import CAKE, CONFIG_DIR, DIST_VERSION_FILE
from . import cake
from . import db
from .env import env
from .log import get as getlog

log = getlog("config")

def _redact(value: str) -> str:
    """Redact a sensitive value for logging: first char + <REDACTED> + last char."""
    if not value or len(value) <= 2:
        return "<REDACTED>"
    return f"{value[0]}<REDACTED>{value[-1]}"


def derive_env_var(setting_name: str) -> str:
    """Derive an env var name from a MISP setting name.

    MISP.redis_host -> MISP_REDIS_HOST
    Plugin.S3_bucket_name -> PLUGIN_S3_BUCKET_NAME
    """
    return setting_name.replace(".", "_").upper()


@dataclass
class SettingSpec:
    """A single setting from settings.yaml."""
    name: str
    default_value: str
    force: bool = False
    blank_protection: bool = False
    since: str = ""
    sensitive: bool = False

    @property
    def env_var(self) -> str:
        """The env var that can override this setting."""
        return derive_env_var(self.name)

    @property
    def env_value(self) -> str | None:
        """The env var value, or None if not set."""
        val = os.environ.get(self.env_var)
        if val is not None and val != "":
            return val
        return None

    @property
    def is_envar(self) -> bool:
        """True if an env var is set for this setting."""
        return self.env_value is not None

    @property
    def effective_value(self) -> str:
        """The value to apply: env var if set, otherwise the default."""
        return self.env_value if self.is_envar else self.default_value

    @property
    def is_sensitive(self) -> bool:
        return self.sensitive

    @property
    def print_value(self) -> str:
        """Value safe for logging. Sensitive values are redacted."""
        return _redact(self.effective_value) if self.sensitive else self.effective_value

    @property
    def setting_type(self) -> str:
        """Backward compat for code that checks setting_type."""
        return "envar" if self.is_envar else "default"

    @classmethod
    def from_dict(cls, name: str, spec: dict) -> SettingSpec:
        """Create from a raw YAML dict entry."""
        raw_value = spec.get("value", "")
        if isinstance(raw_value, bool):
            value = "true" if raw_value else "false"
        else:
            value = str(raw_value)
        return cls(
            name=name,
            default_value=value,
            force=bool(spec.get("force")),
            blank_protection=bool(spec.get("blank_protection")),
            since=spec.get("since", ""),
            sensitive=bool(spec.get("sensitive")),
        )


class SettingsCache:
    """Cache of all current MISP settings from DB + config.php."""

    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.enforced: set[str] = set()
        self.last_defaults_version: str = ""

    def load(self) -> None:
        """Load all settings from system_settings table and config.php."""
        self.settings = {}

        # Load from DB
        raw = db.query("SELECT setting, value FROM system_settings;")
        db_count = 0
        for line in raw.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0]:
                self.settings[parts[0]] = parts[1]
                db_count += 1

        # Load from config.php (covers settings written before system_setting_db was enabled)
        php_count = 0
        try:
            php_code = (
                '<?php require_once "/var/www/MISP/app/Config/config.php"; '
                'echo json_encode($config, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES); ?>'
            )
            result = subprocess.run(
                ["/usr/bin/php"], input=php_code, capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip() not in ("", "null"):
                config = json.loads(result.stdout.strip())
                for key, value in _flatten_dict(config):
                    if key not in self.settings:
                        self.settings[key] = str(value)
                        php_count += 1
        except Exception:
            pass
        log.info("loaded %d DB settings + %d config.php settings", db_count, php_count)

    def load_defaults_version(self) -> None:
        """Load the last-applied defaults version from DB."""
        raw = db.query(
            "SELECT value FROM system_settings WHERE setting='misp_docker.defaults_version';"
        )
        self.last_defaults_version = raw.strip().strip('"')
        image_version = _read_file(DIST_VERSION_FILE, "unknown")
        log.info("defaults version: last applied=%s, image=%s", self.last_defaults_version or "none", image_version)

    def save_defaults_version(self) -> None:
        """Save the current image version as the last-applied defaults version."""
        image_version = _read_file(DIST_VERSION_FILE, "unknown")
        if self.last_defaults_version != image_version:
            log.info("saving defaults version: %s", image_version)
            db.query(
                f"REPLACE INTO system_settings (setting, value) "
                f"VALUES ('misp_docker.defaults_version', '\"{image_version}\"');"
            )

    def get(self, key: str) -> str | None:
        """Get a setting value, or None if not present."""
        return self.settings.get(key)

    def has(self, key: str) -> bool:
        """Check if a setting exists."""
        return key in self.settings

    def normalise(self, value: str | None) -> str | None:
        """Strip JSON encoding from a DB value for comparison."""
        if value is None:
            return None
        s = str(value)
        if s.startswith('"') and s.endswith('"'):
            return s[1:-1]
        return s

    def enforce_envars(self, specs: list[SettingSpec], group: str) -> None:
        """Compare env-var-driven settings against DB, only update what changed."""
        changed = 0
        skipped = 0

        for spec in specs:
            self.enforced.add(spec.name)

            if spec.blank_protection and not spec.effective_value:
                continue

            db_value = self.normalise(self.get(spec.name)) if self.has(spec.name) else "__UNSET__"

            if db_value == spec.effective_value:
                skipped += 1
                continue

            log.info("updating %s '%s' to '%s' (was: %s)", group, spec.name, spec.print_value, str(db_value)[:40])
            cake.set_setting(spec.name, spec.effective_value, force=spec.force)
            changed += 1

        log.info("%s: %d changed, %d unchanged", group, changed, skipped)

    def apply_defaults(self, specs: list[SettingSpec], group: str) -> None:
        """Apply defaults: only if missing, respecting version gates and enforced settings."""
        applied = 0
        upgraded = 0
        skipped = 0
        image_version = _read_file(DIST_VERSION_FILE, "unknown")

        for spec in specs:
            # Env vars always take precedence
            if spec.name in self.enforced:
                skipped += 1
                continue

            # Setting doesn't exist -- always apply
            if not self.has(spec.name):
                log.info("setting new default %s '%s' to '%s'", group, spec.name, spec.print_value)
                cake.set_setting(spec.name, spec.effective_value, force=spec.force)
                applied += 1
                continue

            # Version-gated upgrade
            if (spec.since
                    and _version_newer(spec.since, image_version)
                    and _version_newer(spec.since, self.last_defaults_version)):
                log.info("upgrading default %s '%s' to '%s' (since %s)", group, spec.name, spec.print_value, spec.since)
                cake.set_setting(spec.name, spec.effective_value, force=spec.force)
                upgraded += 1
                continue

            skipped += 1

        if applied > 0 or upgraded > 0:
            log.info("%s defaults: %d new, %d upgraded, %d existing", group, applied, upgraded, skipped)


CUSTOM_YAML = os.path.join(CONFIG_DIR, "custom.yaml")


def load_settings_yaml(path: str | None = None, custom_path: str | None = None) -> dict[str, list[SettingSpec]]:
    """Load settings.yaml and return specs grouped by group name.

    New format: settings.yaml has a top-level 'settings' key with groups as levels.
    Each setting has a 'value' (default) and optional 'force', 'blank_protection', 'since'.

    Env var override is automatic: MISP.redis_host -> MISP_REDIS_HOST.
    If the env var exists, the setting is enforced every startup.

    custom.yaml can add/override settings in any group.
    """
    import yaml

    if path is None:
        path = os.path.join(CONFIG_DIR, "settings.yaml")

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    # New format has top-level 'settings' key
    group_data = raw.get("settings", raw)

    # Merge custom.yaml on top if it exists
    if custom_path is None:
        custom_path = CUSTOM_YAML
    if os.path.exists(custom_path):
        with open(custom_path) as f:
            custom = yaml.safe_load(f) or {}
        custom_settings = custom.get("settings", custom)
        log.info("merging settings from custom.yaml")
        for group_name, settings in custom_settings.items():
            if group_name not in group_data:
                group_data[group_name] = {}
            group_data[group_name].update(settings)

    return _parse_settings(group_data)


def _parse_settings(group_data: dict) -> dict[str, list[SettingSpec]]:
    """Parse grouped YAML data into SettingSpec lists."""
    groups: dict[str, list[SettingSpec]] = {}
    for group_name, settings in group_data.items():
        if not isinstance(settings, dict):
            continue
        specs = []
        for name, raw in settings.items():
            if not isinstance(raw, dict):
                continue
            specs.append(SettingSpec.from_dict(name, raw))
        groups[group_name] = specs
    return groups


def apply_settings_fast(group: str, cache: SettingsCache, all_specs: dict[str, list[SettingSpec]] | None = None) -> None:
    """Apply settings for a group: env-var-driven settings are enforced, others are defaults."""
    if all_specs is None:
        all_specs = load_settings_yaml()

    specs = all_specs.get(group, [])
    envars = [s for s in specs if s.is_envar]
    defaults = [s for s in specs if not s.is_envar]

    if envars:
        cache.enforce_envars(envars, group)
    if defaults:
        cache.apply_defaults(defaults, group)


def needs_minimum_config_write(cache: SettingsCache, all_specs: dict[str, list[SettingSpec]] | None = None) -> bool:
    """Check if minimum_config has any changes that need writing."""
    if all_specs is None:
        all_specs = load_settings_yaml()

    for spec in all_specs.get("minimum_config", []):
        if spec.is_envar:
            db_value = cache.normalise(cache.get(spec.name)) if cache.has(spec.name) else "__UNSET__"
            if db_value != spec.effective_value:
                return True
        else:
            if not cache.has(spec.name):
                return True

    return False


def _version_newer(version: str, reference: str) -> bool:
    """Return True if version > reference. Empty reference means everything is newer."""
    if not reference:
        return True
    if version == reference:
        return False
    def parse(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())
    try:
        return parse(version) > parse(reference)
    except (ValueError, IndexError):
        return version > reference


def _flatten_dict(d: dict, prefix: str = "") -> list[tuple[str, str | int | float]]:
    """Flatten a nested dict into (dotted.key, value) pairs."""
    items: list[tuple[str, str | int | float]] = []
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.extend(_flatten_dict(v, key))
        elif isinstance(v, bool):
            items.append((key, "true" if v else "false"))
        elif isinstance(v, (str, int, float)):
            items.append((key, v))
    return items


def _read_file(path: str, default: str = "") -> str:
    """Read a file's contents, returning default on error."""
    try:
        return Path(path).read_text().strip()
    except (OSError, IOError):
        return default
