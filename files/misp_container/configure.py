"""One-shot MISP configuration: schema, settings, admin user, GPG, auth.

Runs in the configure Job (Kubernetes) or the configure service (Compose)
before any web or worker starts. Sets MISP.live=true when done; web and
worker entrypoints wait for that flag.
"""

import sys

from . import admin
from . import cake
from . import db
from .config import CONDITIONAL_CONFIG_PHP_GROUPS, SettingsCache, apply_settings_fast, load_settings_yaml
from .env import apply_defaults, env
from .init import prepare
from .log import get as getlog, setup as setup_logging

log = getlog("configure")


# The settings groups this step applies to the database, in order. The groups
# init renders into config.php (config.CONFIG_PHP_GROUPS, the S3 group and the
# auth plugins) are not among them: MISP reads those from the file. gpg follows
# the key setup. A plugin group's defaults do nothing until its enable setting
# is true.
DB_GROUPS = ("initialisation", "critical", "optional", "proxy", "kafka", "linotp", "custom_auth", "upstream")


# Values shipped in the chart's secrets-*.env files and the examples. A
# deployment that still carries one of them forgot to set its secrets.
PLACEHOLDER_MARKERS = ("change-me", "override-me", "REPLACE-WITH", "0000000000")
CHECKED_SECRETS = ("SECURITY_SALT", "SECURITY_ENCRYPTION_KEY", "ADMIN_PASSWORD", "ADMIN_KEY",
                   "DB_PASSWORD", "POSTGRES_PASSWORD", "MISP_REDIS_PASSWORD",
                   "GNUPG_PASSWORD")


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


def check_layout() -> None:
    """Refuse a database that holds attribute values as text, as MISP 2.x does."""
    if db.query_ok("SELECT 1 FROM attributes LIMIT 1") and db.has_column("attributes", "value1"):
        log.error("the database holds MISP 2.x data, and this image runs MISP 3.0: import a 2.x database "
                  "with the migrate component, from its MySQL or MariaDB source (docs/migration.md)")
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

        all_specs = load_settings_yaml()

        log.info("core settings")
        for group in DB_GROUPS:
            apply_settings_fast(group, cache, all_specs)

        log.info("admin user")
        admin.setup_admin()

        log.info("GPG")
        apply_settings_fast("gpg", cache, all_specs)

        log.info("plugins")
        for group, switch in CONDITIONAL_CONFIG_PHP_GROUPS:
            if env(switch) == "true":
                log.info("%s enabled (in config.php)", group)

        cache.save_defaults_version()
        log.info("configuration complete")
    finally:
        db.release_config_lock()


def record_run(ok: bool) -> None:
    """Count the run in its system_settings row, for the metrics exporter."""
    try:
        db.record_run("configure", ok)
    except Exception as e:
        log.warning("could not record the configure run: %s", e)


def run() -> None:
    check_identity()
    db.wait_for_db()
    db.init_schema()
    check_layout()

    try:
        configure_misp()
        log.info("setting MISP.live = true")
        cake.set_setting("MISP.live", "true")
    except BaseException:
        record_run(False)
        raise
    record_run(True)


def main() -> None:
    """The configure role: runs once per rollout, before web and worker pods serve."""
    setup_logging("configure")
    log.info("MISP configure starting")
    apply_defaults()
    prepare()
    run()
    log.info("MISP configure complete")


if __name__ == "__main__":
    main()
