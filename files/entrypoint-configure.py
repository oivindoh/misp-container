#!/usr/bin/env python3
"""Configure entrypoint: one-shot schema, settings, admin, GPG and auth setup.

Runs once per rollout, before web and worker pods serve. Exits 0 when
MISP.live=true has been set. Runs as UID 1000 (misp) - no root operations.
"""

from misp_container.env import apply_defaults
from misp_container.configure import run
from misp_container.log import setup as setup_logging, get as getlog

setup_logging("configure")
log = getlog("configure")

log.info("MISP configure starting")
apply_defaults()
run()
log.info("MISP configure complete")
