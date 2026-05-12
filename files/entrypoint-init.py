#!/usr/bin/env python3
"""Init container entrypoint.

Populates emptyDir/named volumes from a compressed tarball baked into the image.
Runs as UID 1000 (misp) - no root operations.
"""

from misp_container.env import apply_defaults
from misp_container.init import setup_tmp, populate_files, populate_config
from misp_container.log import setup as setup_logging, get as getlog

setup_logging("init")
log = getlog("init")

log.info("MISP init container starting")

apply_defaults()
setup_tmp()
populate_files()
populate_config()

log.info("init container complete")
