#!/usr/bin/env python3
"""PHP-FPM entrypoint for the misp-web container.

Waits for MySQL, runs configuration, sets MISP.live, then exec's php-fpm.
Runs as UID 1000 (misp) - no root operations.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from string import Template

from misp_container import CAKE, MISP_BASE
from misp_container.env import apply_defaults, env
from misp_container import db
from misp_container import cake
from misp_container.config import SettingsCache, apply_settings_fast, needs_minimum_config_write, load_settings_yaml
from misp_container import admin
from misp_container.log import setup as setup_logging, get as getlog

CUSTOM_SETUP_SCRIPT = "/custom/setup.py"
CUSTOM_PRE_START_SCRIPT = "/custom/pre-start.py"


log = getlog("web")
configure_log = getlog("configure")


def run_custom_script(path: str, label: str) -> None:
    """Run a custom Python script if it exists."""
    if os.path.isfile(path):
        log.info("running custom script: %s (%s)", label, path)
        exec(compile(Path(path).read_text(), path, "exec"), {"__name__": "__custom__"})
        log.info("custom script %s complete", label)


def configure_php():
    """Generate PHP-FPM and php.ini configs from templates."""
    log.info("configuring PHP-FPM")

    # Build Redis session save path
    redis_host = env("MISP_REDIS_HOST")
    if not re.match(r"^\w+://", redis_host):
        redis_host = f"tcp://{redis_host}"
    redis_port = env("MISP_REDIS_PORT")
    redis_pw = env("MISP_REDIS_PASSWORD")

    if not redis_pw:
        session_path = f"{redis_host}:{redis_port}"
    else:
        session_path = f"{redis_host}:{redis_port}?auth={redis_pw}"
    os.environ["SESSION_SAVE_PATH"] = session_path

    # envsubst equivalent: replace ${VAR} in templates
    for template_path, output_path in [
        ("/etc/misp-docker/php.ini.template", "/tmp/misp-php.ini"),
        ("/etc/misp-docker/php-fpm-pool.conf.template", "/tmp/misp-fpm-pool.conf"),
    ]:
        src = Path(template_path)
        if src.exists():
            content = src.read_text()
            # Replace ${VAR} patterns with env var values
            expanded = re.sub(
                r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}',
                lambda m: os.environ.get(m.group(1), ""),
                content,
            )
            Path(output_path).write_text(expanded)

    log.info("PHP-FPM configured")


def redirect_logs():
    """Tail MISP logs to stdout in the background."""
    log_dir = Path(MISP_BASE) / "app/tmp/logs"
    for name in ("error.log", "debug.log"):
        log_file = log_dir / name
        log_file.touch(exist_ok=True)
        subprocess.Popen(
            ["tail", "-F", str(log_file)],
            stdout=sys.stdout,
            stderr=subprocess.DEVNULL,
        )


def configure_misp():
    """Run the full MISP configuration (settings, admin, GPG, auth)."""
    configure_log.info("acquiring configuration lock")
    db.acquire_config_lock()
    try:
        cake.set_setting("MISP.osuser", "misp")
        cake.run_updates()
        cake.run_db_script("highPerformance")

        cache = SettingsCache()
        cache.load()
        cache.load_defaults_version()

        # Load all settings from YAML once
        all_specs = load_settings_yaml()

        # Minimum config (bootstrap settings in config.php)
        configure_log.info("minimum config")
        if needs_minimum_config_write(cache, all_specs):
            cake.set_setting("MISP.system_setting_db", "false")
        apply_settings_fast("minimum_config", cache, all_specs)

        # Core settings
        configure_log.info("core settings")
        for group in ("db_enable", "initialisation", "critical", "optional"):
            apply_settings_fast(group, cache, all_specs)

        # Admin user
        configure_log.info("admin user")
        admin.setup_admin()

        # GPG
        configure_log.info("GPG")
        admin.configure_gnupg()

        # Auth
        configure_log.info("auth")
        admin.configure_oidc()
        admin.configure_ldap()
        admin.configure_custom_auth()

        # Storage and network
        configure_log.info("storage and network")
        if env("PLUGIN_S3_BUCKET_NAME"):
            apply_settings_fast("s3", cache, all_specs)
        if env("PROXY_ENABLE") == "true":
            apply_settings_fast("proxy", cache, all_specs)
        apply_settings_fast("gpg", cache, all_specs)

        cache.save_defaults_version()
        configure_log.info("configuration complete")
    finally:
        db.release_config_lock()


# -- Main --

setup_logging("web")
log.info("MISP web container starting")

apply_defaults()

# Derived env vars: inherit from the primary setting unless explicitly overridden.
# Users only need to set MISP_BASEURL and MISP_EMAIL.
base_url = env("MISP_BASEURL")
if base_url:
    os.environ.setdefault("MISP_EXTERNAL_BASEURL", base_url)
    os.environ.setdefault("SECURITY_REST_CLIENT_BASEURL", base_url)
misp_email = env("MISP_EMAIL", env("ADMIN_EMAIL"))
if misp_email:
    os.environ.setdefault("MISP_CONTACT", misp_email)
    os.environ.setdefault("GNUPG_EMAIL", misp_email)
modules_url = env("PLUGIN_ENRICHMENT_SERVICES_URL")
if modules_url:
    os.environ.setdefault("PLUGIN_IMPORT_SERVICES_URL", modules_url)
    os.environ.setdefault("PLUGIN_EXPORT_SERVICES_URL", modules_url)
    os.environ.setdefault("PLUGIN_ACTION_SERVICES_URL", modules_url)

salt = env("SECURITY_SALT")
if not salt:
    log.warning("SALT is not set -- MISP will auto-generate one, but passwords set "
                "under one salt become invalid after a restart or on another replica. "
                "Set SALT explicitly. Generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
elif len(salt) < 32:
    log.error("SALT is too short (%d bytes, minimum 32). MISP will reject it and "
              "password authentication will fail. Generate a proper salt with: "
              "python3 -c \"import secrets; print(secrets.token_hex(32))\"", len(salt))
    sys.exit(1)
if not env("MISP_UUID"):
    log.warning("UUID is not set -- MISP will auto-generate one, but it must be set "
                "explicitly for server sync to work. Each instance needs a unique, "
                "stable UUID. Generate with: python3 -c \"import uuid; print(uuid.uuid4())\"")


configure_php()

db.wait_for_mysql()
db.init_schema()

# Early custom hook -- runs after DB is ready, before MISP configuration.
# Use for custom schema patches, data imports, or pre-configuration logic.
run_custom_script(CUSTOM_SETUP_SCRIPT, "setup")

configure_misp()

# Mark instance as live
log.info("setting MISP.live = true")
cake.set_setting("MISP.live", "true")

redirect_logs()

# Late custom hook -- runs after all configuration, just before PHP-FPM starts.
# Use for final tweaks, custom integrations, or one-time data seeding.
run_custom_script(CUSTOM_PRE_START_SCRIPT, "pre-start")

log.info("starting PHP-FPM on port 9002")
os.execvp(
    "/usr/sbin/php-fpm8.4",
    ["/usr/sbin/php-fpm8.4", "--fpm-config", "/tmp/misp-fpm-pool.conf", "-c", "/tmp/misp-php.ini", "-F"],
)
