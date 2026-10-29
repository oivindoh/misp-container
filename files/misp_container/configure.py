"""One-shot MISP configuration: schema, updates, admin user and organisation.

Runs in the configure Job before any web or worker pod of its revision serves. MISP
takes its settings from the env the chart renders (MISP_SETTING_*, config.php), not from
this step. The step records the image version and the configuration checksum it ran for;
the pods of that revision wait for both (db.wait_for_configure).
"""

import sys

from . import admin
from . import cake
from . import db
from .config import missing_settings, repository_settings
from .env import env
from .init import prepare
from .log import get as getlog, setup as setup_logging

log = getlog("configure")


# The placeholders of the chart's default secrets (values.yaml). A deployment that
# still carries one of them forgot to set its secrets.
PLACEHOLDER_MARKERS = ("change-me", "override-me")
CHECKED_SECRETS = ("SECURITY_SALT", "SECURITY_ENCRYPTION_KEY", "ADMIN_PASSWORD", "ADMIN_KEY",
                   "DB_PASSWORD", "MISP_REDIS_PASSWORD", "GNUPG_PASSWORD")


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

    key = env("SECURITY_ENCRYPTION_KEY")
    if key and len(key) < 32:
        # MISP refuses every write of a sensitive value under a shorter key
        problems.append(f"SECURITY_ENCRYPTION_KEY is too short ({len(key)} bytes, minimum 32)")

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


def forget_repository_rows() -> None:
    """Delete the rows of the settings MISP takes from its environment layer.

    The layer wins over them anyway; left, they would read as changes made by hand.
    """
    missing = missing_settings()
    for name in sorted(missing):
        log.warning("setting %s has no value in this pod: its Secret lacks the key, so MISP keeps its own", name)
    names = sorted(repository_settings() - missing)
    if names:
        gone = db.execute("DELETE FROM system_settings WHERE setting = ANY(%s)", (names,))
        if gone:
            log.info("deleted %d database rows of settings the repository carries", gone)


def report_manual_settings() -> None:
    """Log each setting that a person changed in MISP, with who and when where MISP logged it."""
    for row in db.manual_settings(repository_settings()):
        log.warning("setting %s was changed outside the repository%s: move it into git or undo it",
                    row["setting"], f" ({row['changed']})" if row.get("changed") else "")


def configure_misp() -> None:
    """Database updates, the admin user and organisation, and the settings bookkeeping."""
    cake.run_updates()
    log.info("admin user")
    admin.setup_admin()
    forget_repository_rows()
    report_manual_settings()
    # MISP.live is MISP's own maintenance switch: set once, then the admin's
    if db.get_system_setting("MISP.live") is None:
        log.info("setting MISP.live = true")
        cake.set_setting("MISP.live", "true")
    log.info("configuration complete")


def record_run(ok: bool) -> None:
    """Count the run in its system_settings row, for the metrics exporter."""
    try:
        db.record_run("configure", ok)
    except Exception as e:
        log.warning("could not record the configure run: %s", e)


def run() -> None:
    check_identity()
    db.wait_for_db()
    # Two configure runs at once, or a migrate Job beside one, would import the
    # schema twice: the whole run holds the lock
    db.acquire_config_lock()
    try:
        db.init_schema()
        check_layout()
        configure_misp()
        db.record_configured()
    except BaseException:
        record_run(False)
        raise
    finally:
        db.release_config_lock()
    record_run(True)


def main() -> None:
    """The configure role: runs once per revision, before its web and worker pods serve."""
    setup_logging("configure")
    log.info("MISP configure starting")
    prepare()
    run()
    log.info("MISP configure complete")


if __name__ == "__main__":
    main()
