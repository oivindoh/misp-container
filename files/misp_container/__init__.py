"""MISP container entrypoint library."""

MISP_BASE = "/var/www/MISP"
CAKE = f"{MISP_BASE}/app/Console/cake"
CONFIG_DIR = "/etc/misp-docker"
CONFIG_DEFAULTS = "/srv/misp-config"
DIST_VERSION_FILE = "/srv/misp-dist-version"
# MISP's BackgroundJobsTool::VALID_QUEUES without the scheduler queue, which
# nothing enqueues on. scripts/check_upstream.py fails when MISP changes it.
WORKER_QUEUES = ("default", "prio", "email", "update", "cache")
