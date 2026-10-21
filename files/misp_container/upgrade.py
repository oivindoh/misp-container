"""Convert a MISP 2.5 database to MISP 3.0 with cake Admin runUpgrade.

Usage: python3 -m misp_container.upgrade

Runs before the configure step: an init container of the configure Job (the
chart's upgrade component), or the Compose upgrade service. The conversion is
one-way and takes MISP offline, so web and worker must not run: the upgrade
component scales them to 0. It starts only with UPGRADE_BACKUP_CONFIRMED=true.

A database whose attributes table has no value1 column needs nothing: the
step exits 0 at once on a converted database and on an empty one. A stopped
run leaves MISP offline; the next run carries on where it stopped.

Exit codes: 0 converted, or nothing to convert; 1 runUpgrade failed; 2 the
backup is not confirmed.
"""

from __future__ import annotations

import subprocess
import sys
import time

from . import CAKE, cake, db
from .env import apply_defaults, env
from .init import prepare_config, setup_tmp
from .log import setup as setup_logging, get as getlog

log = getlog("upgrade")

EXIT_FAILED = 1
EXIT_UNCONFIRMED = 2


def needs_conversion() -> bool:
    """Whether the database still holds attribute values as text, as MISP 2.5 does."""
    if not db.query_ok("SELECT 1 FROM attributes LIMIT 1"):
        return False
    return db.has_column("attributes", "value1")


def record(status: str, started: float, error: str = "") -> None:
    from .metrics import init_sync_log_table, log_sync_result
    try:
        init_sync_log_table()
        log_sync_result(operation="upgrade", status=status,
                        duration_seconds=time.monotonic() - started, error_message=error[:1000])
    except Exception as e:
        log.warning("could not record the upgrade run: %s", e)


def run() -> None:
    started = time.monotonic()
    if env("UPGRADE_BACKUP_CONFIRMED") != "true":
        log.error("the conversion to MISP 3.0 is one-way: back the database up, check that the backup "
                  "restores, then set UPGRADE_BACKUP_CONFIRMED=true (upgrade.backupConfirmed in the chart)")
        sys.exit(EXIT_UNCONFIRMED)
    db.wait_for_db()
    if not needs_conversion():
        log.info("the database needs no conversion: it is empty or at MISP 3.0 already")
        return

    setup_tmp()
    prepare_config()
    db.acquire_config_lock()
    try:
        if db.is_postgres():
            # MISP 3.0 indexes values with trigrams; a database owner may create this trusted extension
            db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        cake.set_setting("MISP.osuser", "misp")
        log.info("converting the database to MISP 3.0 (cake Admin runUpgrade); on a large instance this takes hours")
        result = subprocess.run([CAKE, "Admin", "runUpgrade"])
        if result.returncode != 0 or needs_conversion():
            log.error("runUpgrade did not finish (exit %d); MISP stays offline. Fix what it names above and run "
                      "the upgrade again: it carries on where it stopped", result.returncode)
            record("error", started, f"runUpgrade exit {result.returncode}")
            sys.exit(EXIT_FAILED)
    finally:
        db.release_config_lock()
    record("success", started)
    log.info("database converted to MISP 3.0 in %ds; the configure step configures it, and the workers "
             "rebuild the correlations once they run", int(time.monotonic() - started))


def main() -> None:
    setup_logging("upgrade")
    apply_defaults()
    run()


if __name__ == "__main__":
    main()
