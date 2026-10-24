"""Nightly deletes in MISP's jobs, logs and audit_logs tables.

Usage: python3 -m misp_container.housekeeping <jobs|logs|audit_logs>

Batched deletes keep locks short. Retention comes from HOUSEKEEPING_<TABLE>_DAYS
(defaults: jobs 2, logs 30, audit_logs 90).
"""

import sys

from . import db
from .env import env
from .log import setup as setup_logging, get as getlog

log = getlog("housekeeping")

TABLES = {
    "jobs": ("date_created", 2),
    "logs": ("created", 30),
    "audit_logs": ("created", 90),
}
BATCH = 25000


def retention_days(table: str) -> int:
    """HOUSEKEEPING_<TABLE>_DAYS, or the table's default when unset or empty."""
    value = env(f"HOUSEKEEPING_{table.upper()}_DAYS").strip()
    return int(value) if value else TABLES[table][1]


def delete_batch(table: str, column: str, days: int) -> int:
    # PostgreSQL's DELETE takes no LIMIT
    return db.execute(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} "
                      f"WHERE {column} < NOW() - INTERVAL '{days} days' ORDER BY id LIMIT {BATCH})")


def run(table: str) -> int:
    column, _ = TABLES[table]
    days = retention_days(table)
    total = 0
    while True:
        deleted = delete_batch(table, column, days)
        total += deleted
        log.info("%s: deleted %d rows older than %d days", table, deleted, days)
        if deleted < BATCH:
            break
    log.info("%s: %d rows deleted in total", table, total)
    return total


def main(argv: list[str]) -> None:
    setup_logging("housekeeping")
    if len(argv) != 1 or argv[0] not in TABLES:
        log.error("usage: python3 -m misp_container.housekeeping <%s>", "|".join(TABLES))
        sys.exit(2)
    db.wait_for_db(retries=12, wait_seconds=5)
    run(argv[0])


if __name__ == "__main__":
    main(sys.argv[1:])
