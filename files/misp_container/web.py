"""MISP's web role: prepares app/Config, waits for the configure Job, then execs PHP-FPM.

PHP fills the PHP-FPM configuration from the environment. Runs as UID 1000 (misp).
"""

import os
import re
import subprocess
import sys
import urllib.parse

from . import db
from .env import apply_defaults, env
from .init import check_writable, prepare
from .log import get as getlog, setup as setup_logging

log = getlog("web")

PHP_INI_DIR = "/etc/misp-docker/php.d"


def set_session_save_path():
    """SESSION_SAVE_PATH for php.ini: PHP keeps the sessions in Redis."""
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


def start_log_relay():
    """Relay the log files MISP writes directly to stdout; it outlives the exec of PHP-FPM."""
    subprocess.Popen([sys.executable, "-m", "misp_container.logrelay"], stdout=sys.stdout, stderr=sys.stderr)


def main():
    setup_logging("web")
    log.info("MISP web container starting")
    apply_defaults()
    prepare()
    set_session_save_path()
    if not env("PLUGIN_S3_BUCKET_NAME"):
        check_writable(env("MISP_ATTACHMENTS_DIR"), "attachments")
    db.wait_for_db()
    db.wait_for_configure()
    start_log_relay()
    log.info("starting PHP-FPM on 127.0.0.1:9002")
    # Debian's production php.ini first, then our overrides: a leading colon appends
    # the directory to PHP's own scan path
    os.environ["PHP_INI_SCAN_DIR"] = f":{PHP_INI_DIR}"
    os.execvp(
        "/usr/sbin/php-fpm8.4",
        ["/usr/sbin/php-fpm8.4", "--fpm-config", "/etc/misp-docker/php-fpm-pool.conf", "-F"],
    )


if __name__ == "__main__":
    main()
