#!/usr/bin/env python3
"""Org sync entrypoint.

Waits for MISP to answer, then applies the declarative org config (orgs.yaml)
through the REST API. Runs as the org-sync Job in Kubernetes, the sync service
in Compose, or by hand: python3 /entrypoint-sync.py [/path/to/orgs.yaml]
"""

import sys
import time
import urllib.request
import urllib.error

from misp_container.env import apply_defaults, env
from misp_container.sync import ConfigError, apply, is_empty, load_config
from misp_container.api import MISPClient
from misp_container.log import setup as setup_logging, get as getlog

setup_logging("sync")
log = getlog("sync")


def wait_for_login_page(base_url: str, timeout: int = 360) -> None:
    """Exit 1 unless the login page answers 200 within the timeout."""
    log.info("waiting for MISP at %s", base_url)
    deadline = time.monotonic() + timeout
    while True:
        try:
            req = urllib.request.Request(f"{base_url.rstrip('/')}/users/login", headers={"Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log.info("MISP is ready")
                    return
        except Exception:
            pass
        if time.monotonic() > deadline:
            log.error("MISP did not answer within %d s", timeout)
            sys.exit(1)
        time.sleep(3)


log.info("MISP org sync starting")
apply_defaults()

config_file = sys.argv[1] if len(sys.argv) > 1 else None
base_url = env("SYNC_BASE_URL", env("MISP_BASEURL"))
api_key = env("ADMIN_KEY")

if not api_key:
    log.info("ADMIN_KEY not set, skipping org sync")
    sys.exit(0)

try:
    config = load_config(file_path=config_file)
except (ConfigError, ValueError) as e:
    log.error("cannot load the org config: %s", e)
    sys.exit(1)
if is_empty(config):
    log.info("no org config to apply, exiting")
    sys.exit(0)

wait_for_login_page(base_url)

# The configure step and the migrate Job hold this lock while they write; a
# sync that starts during one waits for it
from misp_container.db import acquire_config_lock, release_config_lock

acquire_config_lock()
try:
    client = MISPClient(base_url, api_key)
    summary = apply(client=client, config=config)
finally:
    release_config_lock()

errors = [v for v in summary.values() if isinstance(v, dict) and "error" in v]

# The run's count in its system_settings row, for the metrics exporter
try:
    from misp_container.db import record_run

    record_run("org-sync", not errors)
except Exception as e:
    log.warning("could not record the sync run: %s", e)

if errors:
    log.error("sync completed with errors: %s", summary)
    sys.exit(1)

log.info("sync completed successfully")
