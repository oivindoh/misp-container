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
from .config import SettingsCache, apply_settings_fast, load_settings_yaml
from .env import env
from .log import get as getlog

log = getlog("configure")

CUSTOM_SETUP_SCRIPT = "/custom/setup.py"


def run_custom_script(path: str, label: str) -> None:
    """Run a custom Python script if it exists."""
    if os.path.isfile(path):
        log.info("running custom script: %s (%s)", label, path)
        exec(compile(Path(path).read_text(), path, "exec"), {"__name__": "__custom__"})
        log.info("custom script %s complete", label)


def check_identity() -> None:
    """Salt and UUID must be explicit and stable across replicas."""
    salt = env("SECURITY_SALT")
    if not salt:
        log.warning("SECURITY_SALT is not set -- MISP will auto-generate one, but passwords set "
                    "under one salt become invalid after a restart or on another replica. "
                    "Generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
    elif len(salt) < 32:
        log.error("SECURITY_SALT is too short (%d bytes, minimum 32). MISP will reject it and "
                  "password authentication will fail. Generate a proper salt with: "
                  "python3 -c \"import secrets; print(secrets.token_hex(32))\"", len(salt))
        sys.exit(1)
    if not env("MISP_UUID"):
        log.warning("MISP_UUID is not set -- MISP will auto-generate one, but it must be set "
                    "explicitly for server sync to work. Each instance needs a unique, "
                    "stable UUID. Generate with: python3 -c \"import uuid; print(uuid.uuid4())\"")


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

        # minimum_config and db_enable live in config.php, rendered by init in every pod
        log.info("core settings")
        for group in ("initialisation", "critical", "optional"):
            apply_settings_fast(group, cache, all_specs)

        log.info("admin user")
        admin.setup_admin()

        log.info("GPG")
        admin.configure_gnupg()

        log.info("auth")
        admin.configure_oidc()
        admin.configure_ldap()
        admin.configure_custom_auth()

        log.info("storage and network")
        if env("PLUGIN_S3_BUCKET_NAME"):
            apply_settings_fast("s3", cache, all_specs)
        if env("PROXY_ENABLE") == "true":
            apply_settings_fast("proxy", cache, all_specs)
        apply_settings_fast("gpg", cache, all_specs)

        cache.save_defaults_version()
        log.info("configuration complete")
    finally:
        db.release_config_lock()


def run() -> None:
    check_identity()
    db.wait_for_mysql()
    db.init_schema()

    # Early custom hook: after the DB is ready, before MISP configuration.
    run_custom_script(CUSTOM_SETUP_SCRIPT, "setup")

    configure_misp()

    log.info("setting MISP.live = true")
    cake.set_setting("MISP.live", "true")
