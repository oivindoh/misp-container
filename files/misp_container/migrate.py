"""Copy an existing MISP database (MySQL or MariaDB) into this deployment.

Usage: python3 -m misp_container.migrate

Source: MIGRATE_SOURCE_HOST, MIGRATE_SOURCE_PORT (3306), MIGRATE_SOURCE_NAME
(misp), MIGRATE_SOURCE_USER, MIGRATE_SOURCE_PASSWORD, MIGRATE_SOURCE_TLS.
Target: the deployment's DB_* connection, on either engine. Files:
MIGRATE_SOURCE_FILES names a mounted copy of the source's attachments
directory (MISP.attachments_dir, app/files by default); its event directories
go to this deployment's attachments volume.

Same engine (target mysql): every table is created from the source's own
CREATE TABLE and copied whole; the configure step then runs MISP's schema
updates on it, as on any upgrade. Cross engine (target postgres): the target
gets this image's PostgreSQL baseline, the source must be at the same schema
version, and the rows are copied into it column by column.

Exit codes: 0 done; 1 copy failed; 2 configuration; 3 target not empty
(MIGRATE_FORCE=true drops it first); 4 identity mismatch; 5 source schema
differs from this image's (cross engine only).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import MISP_BASE, db
from .env import env
from .log import setup as setup_logging, get as getlog

log = getlog("migrate")

EXIT_COPY = 1
EXIT_CONFIG = 2
EXIT_NOT_EMPTY = 3
EXIT_IDENTITY = 4
EXIT_SCHEMA = 5

BATCH = 2000

# Settings whose value on the source must be the value this deployment
# carries in its env: with another salt no password verifies, with another
# encryption key no stored authkey decrypts, with another UUID no sync
# partner recognises the instance.
IDENTITY = {
    "Security.salt": "SECURITY_SALT",
    "Security.encryption_key": "SECURITY_ENCRYPTION_KEY",
    "MISP.uuid": "MISP_UUID",
}

# MySQL 8 collations that MariaDB does not know.
COLLATIONS = {
    "utf8mb4_0900_ai_ci": "utf8mb4_unicode_ci",
    "utf8mb4_0900_as_cs": "utf8mb4_bin",
    "utf8mb4_0900_bin": "utf8mb4_bin",
}

# Tables the PostgreSQL baseline seeds for this image's version; the target's
# rows are the truth for them, not the source's.
CROSS_ENGINE_KEEP = {"admin_settings", "schema_migrations"}



# -- Configuration -------------------------------------------------------------

def source_settings() -> dict:
    """The source connection, in the shape of db.settings()."""
    host = env("MIGRATE_SOURCE_HOST")
    if not host:
        log.error("MIGRATE_SOURCE_HOST is not set")
        sys.exit(EXIT_CONFIG)
    return {
        "host": host,
        "port": int(env("MIGRATE_SOURCE_PORT", "3306")),
        "user": env("MIGRATE_SOURCE_USER"),
        "password": env("MIGRATE_SOURCE_PASSWORD"),
        "database": env("MIGRATE_SOURCE_NAME", "misp"),
        "tls": env("MIGRATE_SOURCE_TLS") == "true",
    }


def quote(name: str, target_engine: str) -> str:
    return f'"{name}"' if target_engine == db.POSTGRES else f"`{name}`"


# -- Source inspection ---------------------------------------------------------

def source_tables(conn) -> list[str]:
    with db._cursor(conn) as cur:
        cur.execute("SHOW TABLES")
        return [row[0] for row in cur.fetchall()]


def source_columns(conn, table: str) -> list[str]:
    with db._cursor(conn) as cur:
        cur.execute(f"SHOW COLUMNS FROM `{table}`")
        return [row[0] for row in cur.fetchall()]


def source_create_table(conn, table: str) -> str:
    with db._cursor(conn) as cur:
        cur.execute(f"SHOW CREATE TABLE `{table}`")
        return cur.fetchone()[1]


def rewrite_ddl(ddl: str) -> str:
    """A MySQL CREATE TABLE that MariaDB accepts."""
    for old, new in COLLATIONS.items():
        ddl = ddl.replace(old, new)
    return ddl


def source_system_settings(conn, names) -> dict:
    """Values of system_settings rows, JSON-decoded, for the names given."""
    placeholders = ", ".join(["%s"] * len(names))
    with db._cursor(conn) as cur:
        try:
            cur.execute(f"SELECT setting, value FROM system_settings WHERE setting IN ({placeholders})",
                        tuple(names))
        except Exception as e:
            log.warning("source has no readable system_settings table: %s", e)
            return {}
        out = {}
        for setting, value in cur.fetchall():
            if isinstance(value, (bytes, bytearray)):
                value = bytes(value).decode("utf-8", "replace")
            try:
                out[setting] = json.loads(value)
            except (TypeError, ValueError):
                out[setting] = value
        return out


def source_schema_state(conn) -> tuple[str, set[str]]:
    """db_version and the applied ledger migrations of a source."""
    version = ""
    applied: set[str] = set()
    with db._cursor(conn) as cur:
        cur.execute("SELECT value FROM admin_settings WHERE setting = 'db_version'")
        row = cur.fetchone()
        if row:
            version = str(row[0])
        try:
            cur.execute("SELECT migration_id FROM schema_migrations WHERE status = 'applied'")
            applied = {row[0] for row in cur.fetchall()}
        except Exception:
            applied = set()
    return version, applied


# -- Target inspection ---------------------------------------------------------

def target_tables(conn, target_engine: str) -> list[str]:
    with db._cursor(conn) as cur:
        if target_engine == db.POSTGRES:
            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'")
        else:
            cur.execute("SHOW TABLES")
        return [row[0] for row in cur.fetchall()]


def target_column_types(conn, table: str, target_engine: str) -> dict[str, str]:
    """column -> data type, as information_schema reports it."""
    with db._cursor(conn) as cur:
        if target_engine == db.POSTGRES:
            cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = %s "
                        "ORDER BY ordinal_position", (table,))
        else:
            cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = DATABASE() AND table_name = %s "
                        "ORDER BY ordinal_position", (table,))
        return {row[0]: row[1].lower() for row in cur.fetchall()}


def target_schema_state(conn, target_engine: str) -> tuple[str, set[str]]:
    with db._cursor(conn) as cur:
        cur.execute("SELECT value FROM admin_settings WHERE setting = 'db_version'")
        row = cur.fetchone()
        version = str(row[0]) if row else ""
        cur.execute("SELECT migration_id FROM schema_migrations WHERE status = 'applied'")
        applied = {row[0] for row in cur.fetchall()}
    return version, applied


def drop_all(conn, tables: list[str], target_engine: str) -> None:
    with db._cursor(conn) as cur:
        for table in tables:
            if target_engine == db.POSTGRES:
                cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
            else:
                cur.execute(f"DROP TABLE IF EXISTS `{table}`")
    log.info("dropped %d tables from the target", len(tables))


# -- Row conversion ------------------------------------------------------------

def to_bool(v):
    """A tinyint(1) value as the boolean the PostgreSQL column takes."""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode()
    if isinstance(v, str):
        return v not in ("", "0")
    return bool(v)


def to_timestamp(v):
    """MySQL's zero date has no PostgreSQL value."""
    return None if isinstance(v, str) and v.startswith("0000-00-00") else v


def to_bytes(v):
    return bytes(v) if isinstance(v, (bytearray, memoryview)) else v


def coercer(data_type: str):
    """The conversion a MySQL value needs for a PostgreSQL column of this
    type, or None when the value passes through."""
    if data_type == "boolean":
        return to_bool
    if data_type in ("timestamp without time zone", "timestamp with time zone", "date"):
        return to_timestamp
    if data_type == "bytea":
        return to_bytes
    return None


def coerce_row(row, coercers) -> tuple:
    if not coercers:
        return tuple(row)
    return tuple(fn(v) if fn else v for v, fn in zip(row, coercers))


def insert_sql(table: str, columns: list[str], target_engine: str) -> str:
    cols = ", ".join(quote(c, target_engine) for c in columns)
    marks = ", ".join(["%s"] * len(columns))
    return f"INSERT INTO {quote(table, target_engine)} ({cols}) VALUES ({marks})"


def copy_table(src, dst, table: str, columns: list[str], target_engine: str,
               types: dict[str, str] | None = None) -> int:
    """Stream every row of a table from the source into the target."""
    coercers = None
    if target_engine == db.POSTGRES and types:
        coercers = [coercer(types.get(c, "")) for c in columns]
        if not any(coercers):
            coercers = None
    sql = insert_sql(table, columns, target_engine)
    select = "SELECT " + ", ".join(f"`{c}`" for c in columns) + f" FROM `{table}`"
    import pymysql.cursors
    total = 0
    read = src.cursor(pymysql.cursors.SSCursor)
    try:
        read.execute(select)
        with db._cursor(dst) as write:
            while True:
                rows = read.fetchmany(BATCH)
                if not rows:
                    break
                write.executemany(sql, [coerce_row(r, coercers) for r in rows])
                total += len(rows)
    finally:
        read.close()
    return total


def reset_sequences(conn, tables: list[str]) -> None:
    """Move every id sequence past the copied ids (PostgreSQL)."""
    with db._cursor(conn) as cur:
        for table in tables:
            cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,))
            row = cur.fetchone()
            if not row or not row[0]:
                continue
            cur.execute(f'SELECT setval(%s, COALESCE(MAX("id"), 1), MAX("id") IS NOT NULL) FROM "{table}"',
                        (row[0],))


# -- Checks --------------------------------------------------------------------

def check_identity(source_values: dict, environment=None) -> list[str]:
    """Identity settings the source carries that differ from this deployment's."""
    environment = os.environ if environment is None else environment
    problems = []
    for setting, key in IDENTITY.items():
        if setting not in source_values:
            log.warning("%s is not in the source database (it lives in the source's config.php); "
                        "carry it into %s by hand", setting, key)
            continue
        if str(source_values[setting]) != environment.get(key, ""):
            problems.append(f"{setting} on the source differs from {key} in this deployment")
    return problems


def plan_files(source: Path) -> list[tuple[Path, Path]]:
    """(source dir, target dir) pairs: the event directories of a mounted
    attachments directory. Everything else under app/files ships in the image."""
    plan = []
    if not source.is_dir():
        return plan
    attachments = Path(env("MISP_ATTACHMENTS_DIR", f"{MISP_BASE}/app/attachments"))
    for entry in sorted(source.iterdir()):
        if entry.is_dir() and entry.name.isdigit():
            plan.append((entry, attachments / entry.name))
    return plan


def copy_files(source: Path) -> int:
    """Copy the event attachments from a mounted attachments directory."""
    copied = 0
    for src, dst in plan_files(source):
        dst.mkdir(parents=True, exist_ok=True)
        for path in src.rglob("*"):
            if path.is_file():
                rel = path.relative_to(src)
                (dst / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, dst / rel)
                copied += 1
    return copied


# -- Paths ---------------------------------------------------------------------

def migrate_same_engine(src, dst) -> int:
    tables = source_tables(src)
    log.info("creating %d tables on the target from the source's definitions", len(tables))
    with db._cursor(dst) as cur:
        for table in tables:
            cur.execute(rewrite_ddl(source_create_table(src, table)))
    rows = 0
    for table in tables:
        n = copy_table(src, dst, table, source_columns(src, table), db.MYSQL)
        log.info("%s: %d rows", table, n)
        rows += n
    return rows


def migrate_cross_engine(src, dst) -> int:
    db.init_schema()
    src_version, src_applied = source_schema_state(src)
    dst_version, dst_applied = target_schema_state(dst, db.POSTGRES)
    if src_version != dst_version or src_applied != dst_applied:
        log.error("the source schema (db_version %s, %d ledger migrations) is not this image's "
                  "(db_version %s, %d); migrate it onto the mariadb component first, let the "
                  "configure step upgrade it, then migrate from there",
                  src_version or "unknown", len(src_applied), dst_version, len(dst_applied))
        sys.exit(EXIT_SCHEMA)
    targets = set(target_tables(dst, db.POSTGRES))
    rows = 0
    copied = []
    for table in source_tables(src):
        if table in CROSS_ENGINE_KEEP:
            continue
        if table not in targets:
            log.warning("%s: not in this image's schema, skipped", table)
            continue
        types = target_column_types(dst, table, db.POSTGRES)
        columns = [c for c in source_columns(src, table) if c in types]
        missing = [c for c in source_columns(src, table) if c not in types]
        if missing:
            log.warning("%s: columns %s are not in this image's schema, skipped", table, ", ".join(missing))
        with db._cursor(dst) as cur:
            cur.execute(f'TRUNCATE TABLE "{table}"')
        n = copy_table(src, dst, table, columns, db.POSTGRES, types)
        log.info("%s: %d rows", table, n)
        rows += n
        copied.append(table)
    reset_sequences(dst, copied)
    return rows


def record(status: str, started: float, error: str = "") -> None:
    from .metrics import init_sync_log_table, log_sync_result
    try:
        init_sync_log_table()
        log_sync_result(operation="migrate", status=status,
                        duration_seconds=time.monotonic() - started, error_message=error[:1000])
    except Exception as e:
        log.warning("could not record the migration run: %s", e)


def run() -> None:
    started = time.monotonic()
    source = source_settings()
    target_engine = db.engine()
    log.info("migrating %s@%s:%s/%s (mysql) into %s at %s", source["user"], source["host"],
             source["port"], source["database"], target_engine, db.settings()["host"])

    db.wait_for_db()
    try:
        src = db.connect_to(source, db.MYSQL)
    except Exception as e:
        log.error("cannot connect to the source: %s", e)
        sys.exit(EXIT_CONFIG)

    problems = check_identity(source_system_settings(src, list(IDENTITY)))
    for problem in problems:
        log.error("%s", problem)
    if problems:
        log.error("refusing to migrate: the copied passwords, authkeys and sync relationships "
                  "would not work under this deployment's identity")
        sys.exit(EXIT_IDENTITY)

    db.acquire_config_lock()
    dst = db._connect()
    try:
        existing = target_tables(dst, target_engine)
        if existing:
            if env("MIGRATE_FORCE") != "true":
                log.error("the target database has %d tables; set MIGRATE_FORCE=true to drop them",
                          len(existing))
                sys.exit(EXIT_NOT_EMPTY)
            drop_all(dst, existing, target_engine)
        try:
            if target_engine == db.POSTGRES:
                rows = migrate_cross_engine(src, dst)
            else:
                rows = migrate_same_engine(src, dst)
        except SystemExit:
            raise
        except Exception as e:
            log.error("copy failed: %s", e)
            record("error", started, repr(e))
            sys.exit(EXIT_COPY)
        log.info("database copied: %d rows", rows)
        # Web and worker pods wait for MISP.live; the configure step sets it
        # again once the copy is upgraded and configured.
        db.set_system_setting("MISP.live", "false")

        files = env("MIGRATE_SOURCE_FILES")
        if files:
            n = copy_files(Path(files))
            log.info("attachments copied from %s: %d files", files, n)
        else:
            log.info("MIGRATE_SOURCE_FILES is not set; attachments are not copied")
    finally:
        dst.close()
        src.close()
        db.release_config_lock()
    record("success", started)
    log.info("migration complete in %ds; the configure step upgrades and configures the copy",
             int(time.monotonic() - started))


def main() -> None:
    setup_logging("migrate")
    run()


if __name__ == "__main__":
    main()
