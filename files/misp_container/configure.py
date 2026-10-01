"""One-shot MISP configuration: schema, settings, admin user, GPG, auth.

Runs in the configure Job (Kubernetes) or the configure service (Compose)
before any web or worker starts. Sets MISP.live=true when done; web and
worker entrypoints wait for that flag.
"""

import os
import sys
from pathlib import Path

from . import admin
from . import cake
from . import db
from .config import CONDITIONAL_CONFIG_PHP_GROUPS, SettingsCache, apply_settings_fast, load_settings_yaml
from .env import env
from .log import get as getlog

log = getlog("configure")

CUSTOM_SETUP_SCRIPT = "/custom/setup.py"

# The settings groups this step applies to the database, in order. The groups
# init renders into config.php (config.CONFIG_PHP_GROUPS, the S3 group and the
# auth plugins) are not among them: MISP reads those from the file. gpg follows
# the key setup. A switched group is applied while its env var is "true".
DB_GROUPS = ("initialisation", "critical", "optional", "upstream")
SWITCHED_GROUPS = {
    "proxy": "PROXY_ENABLE",
    "kafka": "PLUGIN_KAFKA_ENABLE",
    "linotp": "LINOTPAUTH_ENABLED",
    "custom_auth": "CUSTOM_AUTH_ENABLE",
}


def run_custom_script(path: str, label: str) -> None:
    """Run a custom Python script if it exists."""
    if os.path.isfile(path):
        log.info("running custom script: %s (%s)", label, path)
        exec(compile(Path(path).read_text(), path, "exec"), {"__name__": "__custom__"})
        log.info("custom script %s complete", label)


# Values shipped in the chart's secrets-*.env files and the examples. A
# deployment that still carries one of them forgot to set its secrets.
PLACEHOLDER_MARKERS = ("change-me", "override-me", "REPLACE-WITH", "0000000000")
CHECKED_SECRETS = ("SECURITY_SALT", "SECURITY_ENCRYPTION_KEY", "ADMIN_PASSWORD", "ADMIN_KEY",
                   "DB_PASSWORD", "MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "POSTGRES_PASSWORD", "MISP_REDIS_PASSWORD",
                   "GNUPG_PASSWORD", "SIMPLEBACKGROUNDJOBS_SUPERVISOR_PASSWORD")


def is_placeholder(value: str) -> bool:
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


def check_identity() -> None:
    """Secrets must be real, and salt and UUID explicit and stable across replicas."""
    problems = []
    for key in CHECKED_SECRETS:
        if is_placeholder(env(key)):
            problems.append(f"{key} still has its placeholder value")

    salt = env("SECURITY_SALT")
    if not salt:
        problems.append("SECURITY_SALT is not set; passwords set under an auto-generated salt break "
                        "on restart and on other replicas. Generate with: "
                        "python3 -c \"import secrets; print(secrets.token_hex(32))\"")
    elif len(salt) < 32:
        problems.append(f"SECURITY_SALT is too short ({len(salt)} bytes, minimum 32)")

    if not env("MISP_UUID"):
        problems.append("MISP_UUID is not set; each instance needs a unique, stable UUID for server "
                        "sync. Generate with: python3 -c \"import uuid; print(uuid.uuid4())\"")

    for problem in problems:
        log.error("%s", problem)
    if problems:
        log.error("refusing to configure MISP with the settings above")
        sys.exit(1)


def configure_misp() -> None:
    """Run the full MISP configuration (settings, admin, GPG, auth) under the lock."""
    db.acquire_config_lock()
    try:
        cake.set_setting("MISP.osuser", "misp")
        cake.run_updates()
        cake.run_db_script("highPerformance")

        cache = SettingsCache()
        cache.load()
        cache.load_defaults_version()

        all_specs = load_settings_yaml()

        log.info("core settings")
        for group in DB_GROUPS:
            apply_settings_fast(group, cache, all_specs)

        log.info("admin user")
        admin.setup_admin()

        log.info("GPG")
        admin.configure_gnupg()
        apply_settings_fast("gpg", cache, all_specs)

        log.info("plugins")
        for group, switch in CONDITIONAL_CONFIG_PHP_GROUPS:
            if env(switch) == "true":
                log.info("%s enabled (in config.php)", group)
        for group, switch in SWITCHED_GROUPS.items():
            if env(switch) == "true":
                apply_settings_fast(group, cache, all_specs)

        cache.save_defaults_version()
        log.info("configuration complete")
    finally:
        db.release_config_lock()


def record_run(status: str, started: float, error: str = "") -> None:
    """Write the run to misp_container_sync_log for the metrics exporter."""
    from .metrics import init_sync_log_table, log_sync_result
    import time
    try:
        init_sync_log_table()
        log_sync_result(operation="configure", status=status,
                        duration_seconds=time.monotonic() - started, error_message=error[:1000])
    except Exception as e:
        log.warning("could not record the configure run: %s", e)


def run() -> None:
    import time
    started = time.monotonic()
    check_identity()
    db.wait_for_db()
    db.init_schema()

    # Early custom hook: after the DB is ready, before MISP configuration.
    run_custom_script(CUSTOM_SETUP_SCRIPT, "setup")

    try:
        configure_misp()
        log.info("setting MISP.live = true")
        cake.set_setting("MISP.live", "true")
    except BaseException as e:
        record_run("error", started, repr(e))
        raise
    record_run("success", started)
