"""config.php from the MISP_CONFIG_* env vars the chart renders.

The chart resolves the repository's settings (deploy/chart/files/settings.yaml, with the
env value on top) into two kinds of env var. MISP_SETTING_<Category>__<key> is MISP's own
environment layer, which MISP reads above config.php and the database. What MISP reads
before that layer goes into config.php: MISP_CONFIG_<Category>__<key>, and
MISP_CONFIG_<name> for a setting without a category (debug).
"""

from __future__ import annotations

import json
import os
import re

CONFIG_PREFIX = "MISP_CONFIG_"
SETTING_PREFIX = "MISP_SETTING_"


def env_value(raw: str):
    """A setting's env value with the type MISP's EnvSetting gives it.

    true, false and null become their types, a number a number, and a value that
    starts with [ or { is JSON; anything else stays a string.
    """
    value = raw.strip()
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered == "null":
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    if value[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            return raw
    return raw


def config_php_values(environ=None) -> dict:
    """{section: {key: value}} and {name: value} of the MISP_CONFIG_* env vars."""
    environ = os.environ if environ is None else environ
    tree: dict = {}
    for var in sorted(environ):
        if not var.startswith(CONFIG_PREFIX):
            continue
        section, _, key = var[len(CONFIG_PREFIX):].partition("__")
        value = env_value(environ[var])
        if key:
            tree.setdefault(section, {})[key] = value
        else:
            tree[section] = value
    return tree


def repository_settings(environ=None) -> set[str]:
    """The names of the settings MISP takes from its environment layer.

    MISP_REPOSITORY_SETTINGS lists those of settings.yaml, a Secret's included; a
    MISP_SETTING_* variable that the chart's env value sets directly adds its own.
    """
    environ = os.environ if environ is None else environ
    names = {name for name in environ.get("MISP_REPOSITORY_SETTINGS", "").split(",") if name}
    return names | {var[len(SETTING_PREFIX):].replace("__", ".") for var in environ if var.startswith(SETTING_PREFIX)}


def php_literal(value) -> str:
    """Render a Python value as a PHP literal, escaping single-quoted strings."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "array(" + ", ".join(php_literal(v) for v in value) + ")"
    if isinstance(value, dict):
        return "array(" + ", ".join(f"{php_literal(str(k))} => {php_literal(v)}" for k, v in value.items()) + ")"
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def render_config_php(tree: dict) -> str:
    """config.php for a {section: {key: value}} tree."""
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
