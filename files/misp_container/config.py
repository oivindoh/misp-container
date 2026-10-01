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
    kind: str = "str"
    # Known to the engine for env overrides and coverage, but its default is
    # never written: MISP keeps its own default until an env var sets it.
    track_only: bool = False

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
    def print_value(self) -> str:
        """Value safe for logging. Sensitive values are redacted."""
        return _redact(self.effective_value) if self.sensitive else self.effective_value

    @property
    def typed_value(self):
        """The effective value cast to the type the YAML default has.

        Env values arrive as strings; MISP reads config.php with PHP types,
        and a string "false" is truthy there.
        """
        value = self.effective_value
        if self.kind == "bool":
            return value.strip().lower() in ("1", "true", "yes", "on")
        if self.kind == "int":
            try:
                return int(value)
            except ValueError:
                return value
        if self.kind in ("list", "dict"):
            stripped = value.strip()
            if not stripped:
                return [] if self.kind == "list" else {}
            if stripped[0] in "[{":
                return json.loads(stripped)
            # a bare comma-separated list is the same as a JSON list of strings
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @classmethod
    def from_dict(cls, name: str, spec: dict) -> SettingSpec:
        """Create from a raw YAML dict entry."""
        raw_value = spec.get("value", "")
        if isinstance(raw_value, bool):
            value = "true" if raw_value else "false"
            kind = "bool"
        elif isinstance(raw_value, int):
            value = str(raw_value)
            kind = "int"
        elif isinstance(raw_value, (list, dict)):
            # Plugin config (scopes, role_mapper); env overrides are JSON
            value = json.dumps(raw_value)
            kind = "list" if isinstance(raw_value, list) else "dict"
        else:
            value = str(raw_value)
            kind = "str"
        return cls(
            name=name,
            default_value=value,
            force=bool(spec.get("force")),
            blank_protection=bool(spec.get("blank_protection")),
            since=spec.get("since", ""),
            sensitive=bool(spec.get("sensitive")),
            kind=kind,
            track_only=bool(spec.get("track_only")),
        )


class SettingsCache:
    """Cache of all current MISP settings from DB + config.php."""

    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.enforced: set[str] = set()
        self.last_defaults_version: str = ""

    def load(self) -> None:
        """Load all settings from system_settings table and config.php.

        A failed read raises: an empty result would read as "no settings" and
        every default would be applied over what the operator changed.
        """
        self.settings = {}

        # Load from DB
        raw = db.query("SELECT setting, value FROM system_settings;", check=True)
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
            else:
                log.warning("config.php could not be read: %s", result.stderr.strip() or result.stdout.strip())
        except Exception as e:
            log.warning("config.php could not be read: %s", e)
        log.info("loaded %d DB settings + %d config.php settings", db_count, php_count)

    def load_defaults_version(self) -> None:
        """Load the last-applied defaults version from DB."""
        raw = db.query(
            "SELECT value FROM system_settings WHERE setting='misp_docker.defaults_version';", check=True,
        )
        self.last_defaults_version = raw.strip().strip('"')
        image_version = _read_file(DIST_VERSION_FILE, "unknown")
        log.info("defaults version: last applied=%s, image=%s", self.last_defaults_version or "none", image_version)

    def save_defaults_version(self) -> None:
        """Save the current image version as the last-applied defaults version."""
        image_version = _read_file(DIST_VERSION_FILE, "unknown")
        if self.last_defaults_version != image_version:
            log.info("saving defaults version: %s", image_version)
            db.set_system_setting("misp_docker.defaults_version", image_version)

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
            if spec.name in self.enforced or spec.track_only:
                skipped += 1
                continue

            # A blank default with blank_protection leaves MISP's own default in place
            if spec.blank_protection and not spec.effective_value:
                skipped += 1
                continue

            # Setting doesn't exist -- always apply
            if not self.has(spec.name):
                log.info("setting new default %s '%s' to '%s'", group, spec.name, spec.print_value)
                cake.set_setting(spec.name, spec.effective_value, force=spec.force)
                applied += 1
                continue

            # Version-gated upgrade: this image is at or past since, and the last
            # configure run that applied defaults was before it
            if (spec.since
                    and not _version_newer(spec.since, image_version)
                    and _version_newer(spec.since, self.last_defaults_version)):
                log.info("upgrading default %s '%s' to '%s' (since %s)", group, spec.name, spec.print_value, spec.since)
                cake.set_setting(spec.name, spec.effective_value, force=spec.force)
                upgraded += 1
                continue

            skipped += 1

        if applied > 0 or upgraded > 0:
            log.info("%s defaults: %d new, %d upgraded, %d existing", group, applied, upgraded, skipped)


# Generated catalogue of every MISP setting the curated file does not name
# (scripts/update_settings.py --stack). Its entries are track_only.
UPSTREAM_GROUP = "upstream"
UPSTREAM_FILE = "settings-upstream.yaml"


def load_settings_yaml(path: str | None = None) -> dict[str, list[SettingSpec]]:
    """Load settings.yaml, plus the upstream catalogue next to it, grouped by group name.

    settings.yaml has a top-level 'settings' key with groups as levels.
    Each setting has a 'value' (default) and optional 'force', 'blank_protection', 'since'.

    Env var override is automatic: MISP.redis_host -> MISP_REDIS_HOST.
    If the env var exists, the setting is enforced every startup.
    """
    import yaml

    if path is None:
        path = os.path.join(CONFIG_DIR, "settings.yaml")

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    group_data = raw.get("settings", raw)
    groups = _parse_settings(group_data)

    upstream_path = os.path.join(os.path.dirname(path), UPSTREAM_FILE)
    if os.path.exists(upstream_path):
        with open(upstream_path) as f:
            upstream = yaml.safe_load(f) or {}
        curated = {spec.name for specs in groups.values() for spec in specs}
        specs = []
        for name, raw_spec in (upstream.get("settings") or {}).items():
            if name in curated or not isinstance(raw_spec, dict):
                continue
            spec = SettingSpec.from_dict(name, raw_spec)
            spec.track_only = True
            specs.append(spec)
        if specs:
            groups[UPSTREAM_GROUP] = specs

    return groups


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


# Groups that MISP must read from config.php before the database is available.
# MISP ignores the database for SystemSetting::BLOCKED_SETTINGS (salt,
# encryption key, python_bin, attachments_dir, system_setting_db and others),
# so every pod renders these from settings.yaml and env at start.
CONFIG_PHP_GROUPS = ("minimum_config", "db_enable")
# The S3 group joins them while the bucket name is set; nothing else applies it
S3_GROUP, S3_SWITCH = "s3", "PLUGIN_S3_BUCKET_NAME"


def php_literal(value) -> str:
    """Render a Python value as a PHP literal, escaping single-quoted strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "array(" + ", ".join(php_literal(v) for v in value) + ")"
    if isinstance(value, dict):
        return "array(" + ", ".join(f"{php_literal(str(k))} => {php_literal(v)}" for k, v in value.items()) + ")"
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def render_config_php(specs: list[SettingSpec]) -> str:
    """Render config.php from SettingSpecs.

    Setting names split on the first dot into section and key; a name without
    a dot is a top-level key. Blank values with blank_protection are left out
    so MISP applies its own default (for example an auto-generated salt).
    """
    tree: dict = {}
    for spec in specs:
        if spec.blank_protection and not spec.effective_value:
            continue
        section, _, key = spec.name.partition(".")
        value = spec.typed_value
        if key:
            current = tree.setdefault(section, {}).get(key)
            # Security.auth collects one entry per enabled auth plugin
            if isinstance(current, list) and isinstance(value, list):
                value = current + [v for v in value if v not in current]
            tree[section][key] = value
        else:
            tree[section] = value

    lines = ["<?php", "$config = array("]
    for section, value in tree.items():
        if isinstance(value, dict):
            lines.append(f"    '{section}' => array(")
            for key, val in value.items():
                lines.append(f"        '{key}' => {php_literal(val)},")
            lines.append("    ),")
        else:
            lines.append(f"    '{section}' => {php_literal(value)},")
    lines.append(");")
    return "\n".join(lines) + "\n"


# Groups rendered into config.php only when their switch is "true": the auth
# plugins read their config from config.php, never from the database.
CONDITIONAL_CONFIG_PHP_GROUPS = (
    ("oidc", "OIDC_ENABLE"),
    ("ldap", "LDAPAUTH_ENABLE"),
    ("apache_auth", "APACHESECUREAUTH_LDAP_ENABLE"),
)


def config_php_specs(all_specs: dict[str, list[SettingSpec]]) -> list[SettingSpec]:
    """The specs that belong in config.php: bootstrap groups, S3 and the enabled auth plugins."""
    specs: list[SettingSpec] = []
    for group in CONFIG_PHP_GROUPS:
        specs.extend(all_specs.get(group, []))
    if os.environ.get(S3_SWITCH):
        specs.extend(all_specs.get(S3_GROUP, []))
    for group, switch in CONDITIONAL_CONFIG_PHP_GROUPS:
        if os.environ.get(switch, "").lower() == "true":
            specs.extend(all_specs.get(group, []))
    return specs


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
