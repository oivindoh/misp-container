#!/usr/bin/env python3
"""PHP-FPM entrypoint for the misp-web container.

Prepares app/Config, renders the PHP-FPM config, waits for the configure Job (MISP.live=true),
then exec's php-fpm. Runs as UID 1000 (misp) - no root operations.
"""

import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

from misp_container import MISP_BASE
from misp_container.env import apply_defaults, env
from misp_container import db
from misp_container.configure import run_custom_script
from misp_container.init import prepare, check_writable
from misp_container.log import setup as setup_logging, get as getlog

CUSTOM_PRE_START_SCRIPT = "/custom/pre-start.py"


log = getlog("web")


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
        # phpredis parses the part after ? as a query string
        session_path = f"{redis_host}:{redis_port}?auth={urllib.parse.quote(redis_pw, safe='')}"
    os.environ["SESSION_SAVE_PATH"] = session_path

    # envsubst equivalent: replace ${VAR} in templates
    for template_path, output_path in [
        ("/etc/misp-docker/php.ini.template", "/tmp/misp-php.ini"),
        ("/etc/misp-docker/php-fpm-pool.conf.template", "/tmp/misp-fpm-pool.conf"),
    ]:
        src = Path(template_path)
        if src.exists():
            content = src.read_text()
            expanded = re.sub(
                r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}',
                lambda m: os.environ.get(m.group(1), ""),
                content,
            )
            Path(output_path).write_text(expanded)

    log.info("PHP-FPM configured")


def start_log_relay():
    """Relay the log files MISP writes directly to stdout; it outlives the exec of PHP-FPM."""
    subprocess.Popen([sys.executable, "-m", "misp_container.logrelay"], stdout=sys.stdout, stderr=sys.stderr)


# -- Main --

setup_logging("web")
log.info("MISP web container starting")

apply_defaults()
prepare()
configure_php()

if not env("PLUGIN_S3_BUCKET_NAME"):
    check_writable(env("MISP_ATTACHMENTS_DIR"), "attachments")

db.wait_for_db()
db.wait_for_live()

start_log_relay()

# Late custom hook -- runs on every web replica, just before PHP-FPM starts.
run_custom_script(CUSTOM_PRE_START_SCRIPT, "pre-start")

log.info("starting PHP-FPM on port 9002")
os.execvp(
    "/usr/sbin/php-fpm8.4",
    ["/usr/sbin/php-fpm8.4", "--fpm-config", "/tmp/misp-fpm-pool.conf", "-c", "/tmp/misp-php.ini", "-F"],
)
