"""Copy an existing MISP database (MySQL or MariaDB) into this deployment.

Usage: python3 -m misp_container.migrate

Source: MIGRATE_SOURCE_HOST, MIGRATE_SOURCE_PORT (3306), MIGRATE_SOURCE_NAME
(misp), MIGRATE_SOURCE_USER, MIGRATE_SOURCE_PASSWORD, MIGRATE_SOURCE_TLS.
Target: the deployment's DB_* connection, on either engine.

Attachments come from one of two sources: MIGRATE_SOURCE_FILES, a mounted
copy of the source's attachments directory (MISP.attachments_dir, app/files
by default), or MIGRATE_SOURCE_S3_BUCKET with MIGRATE_SOURCE_S3_ENDPOINT,
_REGION, _ACCESS_KEY, _SECRET_KEY, _VALIDATE_CA and _CA. They go to this
deployment's bucket when PLUGIN_S3_BUCKET_NAME is set, and to its attachments
volume otherwise. The Job checks both buckets before it copies the database.

Same engine (target mysql): every table is created from the source's own
CREATE TABLE and copied whole; the configure step then runs MISP's schema
updates on it, as on any upgrade. Cross engine (target postgres): the target
gets this image's PostgreSQL baseline, the source must be at the same schema
version, and the rows are copied into it column by column.

Exit codes: 0 done, or the target holds a copy this Job made (it stays unless
MIGRATE_FORCE=true and MIGRATE_REPLACE_COPY=true); 1 copy failed; 2
configuration; 3 target not empty (MIGRATE_FORCE=true drops it first); 4
identity mismatch; 5 source schema differs from this image's (cross engine
only).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

from . import MISP_BASE, db
from .env import env
from .log import setup as setup_logging, get as getlog
from .s3 import Bucket, S3Error

log = getlog("migrate")

EXIT_COPY = 1
EXIT_CONFIG = 2
EXIT_NOT_EMPTY = 3
EXIT_IDENTITY = 4
EXIT_SCHEMA = 5

BATCH = 2000
# pg8000 sends at most this many parameters in one statement
PG_MAX_PARAMS = 32767

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


def last_copy(conn, tables: list[str]) -> str:
    """When this Job last copied into the target, from the target's sync log; empty when it never did."""
    if "misp_container_sync_log" not in tables:
        return ""
    with db._cursor(conn) as cur:
        cur.execute("SELECT MAX(timestamp) FROM misp_container_sync_log "
                    "WHERE operation = 'migrate' AND status = 'success'")
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else ""


def target_plan(tables: int, copied: str, force: str, replace: str) -> tuple[str, str]:
    """What the Job does with the target: ("copy", ""), ("keep", why) or ("refuse", why).

    The migration runs again on every upgrade or sync while it is enabled, and
    on each retry of the configure Job it runs in. A copy this Job made stays,
    and the run succeeds, so the configure step that follows is not blocked. A
    MIGRATE_FORCE=true left behind does not drop it: only
    MIGRATE_REPLACE_COPY=true as well does.
    """
    if not tables:
        return "copy", ""
    if copied and not (force == "true" and replace == "true"):
        return "keep", (f"the target holds the copy this Job made at {copied}; it stays. Disable the migration "
                        "once it is done; to copy again, set MIGRATE_FORCE=true and MIGRATE_REPLACE_COPY=true")
    if force != "true":
        return "refuse", f"the target database has {tables} tables; set MIGRATE_FORCE=true to drop them"
    return "copy", ""


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


def insert_rows(cur, table: str, columns: list[str], target_engine: str, rows: list[tuple]) -> None:
    """Insert rows in as few statements as the engine takes.

    PyMySQL's executemany already sends one multi-row INSERT; pg8000's sends
    one statement per row, so PostgreSQL gets multi-row VALUES lists instead.
    """
    if target_engine != db.POSTGRES:
        cur.executemany(insert_sql(table, columns, target_engine), rows)
        return
    head = insert_sql(table, columns, target_engine)
    row_marks = head[head.index(" VALUES ") + len(" VALUES "):]
    head = head[:head.index(" VALUES ") + len(" VALUES ")]
    per_statement = max(1, PG_MAX_PARAMS // len(columns))
    for start in range(0, len(rows), per_statement):
        chunk = rows[start:start + per_statement]
        cur.execute(head + ", ".join([row_marks] * len(chunk)), [value for row in chunk for value in row])


def copy_table(src, dst, table: str, columns: list[str], target_engine: str,
               types: dict[str, str] | None = None) -> int:
    """Stream every row of a table from the source into the target."""
    coercers = None
    if target_engine == db.POSTGRES and types:
        coercers = [coercer(types.get(c, "")) for c in columns]
        if not any(coercers):
            coercers = None
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
                insert_rows(write, table, columns, target_engine, [coerce_row(r, coercers) for r in rows])
                total += len(rows)
    finally:
        read.close()
    return total


def secondary_indexes(conn, table: str) -> list[tuple[str, str]]:
    """(name, CREATE INDEX statement) of each PostgreSQL index on a table that backs no constraint."""
    with db._cursor(conn) as cur:
        cur.execute("SELECT i.relname, pg_get_indexdef(x.indexrelid) FROM pg_index x "
                    "JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid "
                    "WHERE t.relname = %s AND t.relnamespace = current_schema()::regnamespace "
                    "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid) "
                    "ORDER BY i.relname", (table,))
        return [(name, ddl) for name, ddl in cur.fetchall()]


def copy_without_indexes(src, dst, table: str, columns: list[str], types: dict[str, str]) -> int:
    """Copy a table into PostgreSQL with its secondary indexes dropped, then build them once.

    PostgreSQL updates every index on each insert; on a table of millions of
    rows that costs more than the copy. A copy that fails leaves the indexes
    out; the rerun with MIGRATE_FORCE=true starts from the baseline again.
    """
    indexes = secondary_indexes(dst, table)
    with db._cursor(dst) as cur:
        for name, _ in indexes:
            cur.execute(f'DROP INDEX "{name}"')
    n = copy_table(src, dst, table, columns, db.POSTGRES, types)
    with db._cursor(dst) as cur:
        for name, ddl in indexes:
            started = time.monotonic()
            cur.execute(ddl)
            if n:
                log.info("%s: index %s rebuilt in %ds", table, name, time.monotonic() - started)
    return n


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


class ConfigError(Exception):
    """The attachment source or target is set up wrong."""


def attachment_key(rel: str) -> str | None:
    """MISP's key for a file under an attachments directory; None for a file that is no attachment.

    MISP stores <event>/<attribute><suffix>, under shadow/ for a proposal, and on
    disk with MISP.attachments_bucketed also under bucket_<n>/. On S3 and in this
    image's attachments volume the key has no bucket level (AttachmentTool::getPath()).
    """
    parts = [p for p in rel.split("/") if p]
    shadow = parts[:1] == ["shadow"]
    if shadow:
        parts = parts[1:]
    if parts and re.fullmatch(r"bucket_\d+", parts[0]):
        parts = parts[1:]
    if len(parts) < 2 or not parts[0].isdigit():
        return None
    return "/".join((["shadow"] if shadow else []) + parts)


def bucket_from_env(name: str, endpoint: str, region: str, access_key: str, secret_key: str,
                    validate_ca: str, ca: str) -> Bucket | None:
    """A Bucket from the env vars named, None when the bucket name is unset."""
    if not env(name):
        return None
    missing = [k for k in (access_key, secret_key) if not env(k)]
    if missing:
        raise ConfigError(f"{name} is set but {' and '.join(missing)} not: the Job signs its S3 requests "
                          "with an access key")
    verify = False if env(validate_ca, "true").lower() == "false" else (env(ca) or True)
    return Bucket(name=env(name), access_key=env(access_key), secret_key=env(secret_key),
                  region=env(region) or "eu-west-1", endpoint=env(endpoint).rstrip("/"), verify=verify)


def attachment_source() -> Path | Bucket | None:
    files = env("MIGRATE_SOURCE_FILES")
    bucket = bucket_from_env("MIGRATE_SOURCE_S3_BUCKET", "MIGRATE_SOURCE_S3_ENDPOINT", "MIGRATE_SOURCE_S3_REGION",
                             "MIGRATE_SOURCE_S3_ACCESS_KEY", "MIGRATE_SOURCE_S3_SECRET_KEY",
                             "MIGRATE_SOURCE_S3_VALIDATE_CA", "MIGRATE_SOURCE_S3_CA")
    if files and bucket:
        raise ConfigError("set MIGRATE_SOURCE_FILES or MIGRATE_SOURCE_S3_BUCKET, not both")
    if files and not Path(files).is_dir():
        raise ConfigError(f"MIGRATE_SOURCE_FILES={files} is no directory: mount the source's attachments there")
    return Path(files) if files else bucket


def attachment_target() -> Path | Bucket:
    """This deployment's bucket when it stores attachments on S3, its attachments volume otherwise."""
    bucket = bucket_from_env("PLUGIN_S3_BUCKET_NAME", "PLUGIN_S3_AWS_ENDPOINT", "PLUGIN_S3_REGION",
                             "PLUGIN_S3_AWS_ACCESS_KEY", "PLUGIN_S3_AWS_SECRET_KEY", "PLUGIN_S3_AWS_VALIDATE_CA",
                             "PLUGIN_S3_AWS_CA")
    # MISP uses the endpoint only for an AWS-compatible store (Plugin.S3_aws_compatible)
    if bucket and env("PLUGIN_S3_AWS_COMPATIBLE", "true").lower() in ("false", "0"):
        bucket.endpoint = ""
    return bucket or Path(env("MISP_ATTACHMENTS_DIR", f"{MISP_BASE}/app/attachments"))


def check_bucket(bucket: Path | Bucket | None) -> None:
    """Fail before the database copy when a bucket cannot be listed."""
    if isinstance(bucket, Bucket):
        next(iter(bucket.keys("0")), None)


def source_attachments(source: Path | Bucket) -> Iterator[tuple[str, str]]:
    """(key, where in the source) for every attachment; other files are left out."""
    if isinstance(source, Bucket):
        for name in source.keys():
            key = attachment_key(name)
            if key:
                yield key, name
        return
    for path in sorted(source.rglob("*")):
        if path.is_file():
            key = attachment_key(path.relative_to(source).as_posix())
            if key:
                yield key, str(path)


def copy_attachments(source: Path | Bucket, target: Path | Bucket) -> int:
    """Copy every attachment from source to target under MISP's key; returns the count."""
    copied = 0
    spool = Path(tempfile.gettempdir()) / "migrate-attachment"
    for key, where in source_attachments(source):
        if isinstance(source, Bucket) and isinstance(target, Bucket):
            source.download(where, spool)
            target.upload(key, spool)
        elif isinstance(source, Bucket):
            source.download(where, target / key)
        elif isinstance(target, Bucket):
            target.upload(key, Path(where))
        else:
            (target / key).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(where, target / key)
        copied += 1
    spool.unlink(missing_ok=True)
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
        n = copy_without_indexes(src, dst, table, columns, types)
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

    try:
        files_from, files_to = attachment_source(), attachment_target()
        check_bucket(files_from)
        check_bucket(files_to)
    except (ConfigError, S3Error) as e:
        log.error("%s", e)
        sys.exit(EXIT_CONFIG)

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
        plan, why = target_plan(len(existing), last_copy(dst, existing), env("MIGRATE_FORCE"),
                                env("MIGRATE_REPLACE_COPY"))
        if plan == "keep":
            log.info("%s", why)
            return
        if plan == "refuse":
            log.error("%s", why)
            sys.exit(EXIT_NOT_EMPTY)
        if existing:
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

        if files_from is None:
            log.info("neither MIGRATE_SOURCE_FILES nor MIGRATE_SOURCE_S3_BUCKET is set; attachments are not copied")
        else:
            try:
                n = copy_attachments(files_from, files_to)
            except (S3Error, OSError) as e:
                log.error("attachment copy failed: %s", e)
                record("error", started, repr(e))
                sys.exit(EXIT_COPY)
            log.info("attachments copied from %s to %s: %d files", files_from, files_to, n)
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
