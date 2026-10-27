"""config.php from the MISP_CONFIG_* env vars the chart renders.

The chart resolves the repository's settings (deploy/chart/files/settings.yaml, with the
env value on top) into two kinds of env var. MISP_SETTING_<Category>__<key> is MISP's own
environment layer, which MISP reads above config.php and the database. What MISP reads
before that layer goes into config.php: MISP_CONFIG_<Category>__<key>, as JSON typed by
the setting's default, or as a plain string for the names in MISP_PLAIN_CONFIG, which a
Secret fills.
"""

from __future__ import annotations

import json
import os

CONFIG_PREFIX = "MISP_CONFIG_"
SETTING_PREFIX = "MISP_SETTING_"
PLAIN = "MISP_PLAIN_CONFIG"


def config_php_values(environ=None) -> dict:
    """{section: {key: value}} and {name: value} of the MISP_CONFIG_* env vars."""
    environ = os.environ if environ is None else environ
    plain = set(environ.get(PLAIN, "").split(","))
    tree: dict = {}
    for var in sorted(environ):
        if not var.startswith(CONFIG_PREFIX):
            continue
        section, _, key = var[len(CONFIG_PREFIX):].partition("__")
        value = environ[var]
        if var not in plain:
            try:
                value = json.loads(value)
            except ValueError:
                # An env value the setting's type does not take, such as a path for a flag
                pass
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


def setting(name: str, default: str = "") -> str:
    """A setting's value as the chart passes it to MISP's environment layer."""
    return os.environ.get(SETTING_PREFIX + name.replace(".", "__"), default)


def missing_settings(environ=None) -> set[str]:
    """The settings MISP_REPOSITORY_SETTINGS lists whose MISP_SETTING_* variable the pod lacks.

    A Secret the release supplies (secrets.create false) can lack a key the secrets
    value names; the setting then never reaches MISP.
    """
    environ = os.environ if environ is None else environ
    listed = {name for name in environ.get("MISP_REPOSITORY_SETTINGS", "").split(",") if name}
    return {name for name in listed if SETTING_PREFIX + name.replace(".", "__") not in environ}


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
