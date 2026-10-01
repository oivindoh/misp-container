"""Database access for the entrypoints, on MySQL/MariaDB or PostgreSQL.

DB_ENGINE selects the engine ("mysql", the default, or "postgres"); DB_HOST,
DB_PORT, DB_NAME, DB_USER, DB_PASSWORD and DB_TLS describe the connection.
The MYSQL_* names are accepted as aliases (env.py). Every query here is either
engine-neutral or built through the helpers below, so callers never branch.
"""

from __future__ import annotations

import sys
import time
from contextlib import closing

from . import env as envmod
from .log import get as getlog

log = getlog("db")

MYSQL = "mysql"
POSTGRES = "postgres"


def engine() -> str:
    value = envmod.env("DB_ENGINE", MYSQL).strip().lower()
    if value in ("postgres", "postgresql", "pgsql"):
        return POSTGRES
    return MYSQL


def is_postgres() -> bool:
    return engine() == POSTGRES


def settings() -> dict:
    """Connection settings from env, with the engine's default port."""
    e = envmod.env
    port = e("DB_PORT") or ("5432" if is_postgres() else "3306")
    return {
        "host": e("DB_HOST"),
        "port": int(port),
        "user": e("DB_USER"),
        "password": e("DB_PASSWORD"),
        "database": e("DB_NAME"),
        "tls": e("DB_TLS") == "true",
    }


# -- Engine-specific SQL fragments -------------------------------------------

def bool_lit(value: bool) -> str:
    """tinyint(1) on MySQL is boolean on PostgreSQL."""
    if is_postgres():
        return "TRUE" if value else "FALSE"
    return "1" if value else "0"


def now_epoch() -> str:
    """Current time as UNIX seconds, as an SQL expression."""
    if is_postgres():
        return "EXTRACT(EPOCH FROM NOW())::bigint"
    return "UNIX_TIMESTAMP()"


def epoch(expr: str) -> str:
    """A timestamp expression as UNIX seconds."""
    if is_postgres():
        return f"EXTRACT(EPOCH FROM {expr})::bigint"
    return f"UNIX_TIMESTAMP({expr})"


def ago(amount: int, unit: str) -> str:
    """NOW() minus an interval; unit is DAY or HOUR."""
    if is_postgres():
        return f"NOW() - INTERVAL '{amount} {unit.lower()}s'"
    return f"NOW() - INTERVAL {amount} {unit.upper()}"


# -- Connections and queries -------------------------------------------------

def _connect(autocommit: bool = True):
    """A DB-API connection from the environment."""
    return connect_to(settings(), engine(), autocommit)


def connect_to(s: dict, target_engine: str, autocommit: bool = True):
    """A DB-API connection to the database that settings() shaped `s` names."""
    if target_engine == POSTGRES:
        import pg8000.dbapi
        kwargs = {"user": s["user"], "password": s["password"], "host": s["host"],
                  "port": s["port"], "database": s["database"]}
        if s["tls"]:
            import ssl
            kwargs["ssl_context"] = ssl.create_default_context()
        conn = pg8000.dbapi.connect(**kwargs)
        conn.autocommit = autocommit
        return conn

    import pymysql
    kwargs = {"host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"],
              "database": s["database"], "autocommit": autocommit, "charset": "utf8mb4"}
    if s["tls"]:
        import ssl
        kwargs["ssl"] = {"ssl": ssl.create_default_context()}
    return pymysql.connect(**kwargs)


def _cursor(conn):
    """A cursor closed on exit; pg8000 cursors are not context managers."""
    return closing(conn.cursor())


class DictCursor:
    """A cursor whose fetchone and fetchall return dicts keyed by column name, closed on exit."""

    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._cur.close()
        return False

    def execute(self, sql, params=None):
        return self._cur.execute(sql, params or ())

    def _row(self, row):
        if row is None or isinstance(row, dict):
            return row
        names = [d[0] for d in (self._cur.description or [])]
        return dict(zip(names, row))

    def fetchone(self):
        return self._row(self._cur.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()]


def dict_cursor(conn) -> DictCursor:
    return DictCursor(conn.cursor())


def _fetch_rows(cur):
    """Rows of the last statement, or [] when it returned no result set."""
    if cur.description is None:
        return []
    return list(cur.fetchall())


def query(sql: str, *, check: bool = False) -> str:
    """Run a statement. Returns rows as tab-separated columns, newline-separated rows."""
    conn = _connect()
    try:
        with _cursor(conn) as cur:
            cur.execute(sql)
            rows = _fetch_rows(cur)
            if not rows:
                return ""
            lines = []
            for row in rows:
                cols = []
                for val in row:
                    if val is None:
                        cols.append("NULL")
                    elif isinstance(val, (bytes, bytearray, memoryview)):
                        cols.append(bytes(val).decode())
                    elif isinstance(val, bool):
                        cols.append("1" if val else "0")
                    else:
                        cols.append(str(val))
                lines.append("\t".join(cols))
            return "\n".join(lines)
    except Exception as e:
        if check:
            raise RuntimeError(f"query failed: {e}") from e
        return ""
    finally:
        conn.close()


def dict_query(sql: str, params=None) -> list[dict]:
    """Run a statement, return rows as dicts keyed by column name."""
    conn = _connect()
    try:
        with _cursor(conn) as cur:
            cur.execute(sql, params or ())
            if cur.description is None:
                return []
            names = [d[0] for d in cur.description]
            rows = []
            for row in cur.fetchall():
                rows.append({names[i]: (bytes(v).decode() if isinstance(v, (bytes, bytearray, memoryview)) else v)
                             for i, v in enumerate(row)})
            return rows
    finally:
        conn.close()


def execute(sql: str, params=None) -> int:
    """Run a statement, return the affected row count."""
    conn = _connect()
    try:
        with _cursor(conn) as cur:
            cur.execute(sql, params or ())
            return cur.rowcount if cur.rowcount is not None else 0
    finally:
        conn.close()


def query_ok(sql: str) -> bool:
    """Run a statement, return True if it succeeded."""
    try:
        conn = _connect()
        try:
            with _cursor(conn) as cur:
                cur.execute(sql)
                _fetch_rows(cur)
            return True
        finally:
            conn.close()
    except Exception:
        return False


def wait_for_db(retries: int = 100, wait_seconds: int = 5) -> None:
    """Wait for the database to accept connections."""
    s = settings()
    log.info("waiting for %s at %s:%s", engine(), s["host"], s["port"])
    for i in range(retries, 0, -1):
        if query_ok("SELECT 1"):
            log.info("database is ready")
            return
        log.info("waiting for database (%d retries left)", i)
        time.sleep(wait_seconds)
    log.error("could not connect to %s at %s:%s", engine(), s["host"], s["port"])
    sys.exit(1)


def set_system_setting(name: str, value: str) -> None:
    """Write a row of system_settings (value stored JSON-encoded, as MISP does)."""
    literal = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    updated = execute("UPDATE system_settings SET value = %s WHERE setting = %s", (literal, name))
    if not updated:
        execute("INSERT INTO system_settings (setting, value) VALUES (%s, %s)", (name, literal))


def wait_for_live(retries: int = 120, wait_seconds: int = 3) -> None:
    """Wait for the configure step of this image version.

    The step sets MISP.live=true and records the image version it configured
    (misp_docker.defaults_version). A pod of a newer image waits for its own
    configure run, whatever tool applied the manifests and in whatever order.
    """
    from . import DIST_VERSION_FILE
    try:
        image_version = open(DIST_VERSION_FILE).read().strip()
    except OSError:
        image_version = ""
    log.info("waiting for the configure step of image %s", image_version or "unknown")
    for i in range(retries, 0, -1):
        rows = query(
            "SELECT setting, value FROM system_settings "
            "WHERE setting IN ('MISP.live', 'misp_docker.defaults_version');"
        )
        state = {}
        for line in rows.splitlines():
            key, _, value = line.partition("\t")
            state[key] = value.strip().strip('"')
        live = state.get("MISP.live") == "true"
        configured = state.get("misp_docker.defaults_version", "")
        if live and (not image_version or configured == image_version):
            log.info("MISP is live and configured for %s", configured or "this image")
            return
        log.info("waiting: live=%s, configured for %s, this image is %s (%d retries left)",
                 live, configured or "nothing", image_version or "unknown", i)
        time.sleep(wait_seconds)
    log.error("the configure step for image %s did not finish within the timeout; "
              "is the configure job running?", image_version or "unknown")
    sys.exit(1)


SCHEMA_FILES = {
    MYSQL: "/var/www/MISP/INSTALL/MYSQL.sql",
    POSTGRES: "/var/www/MISP/INSTALL/POSTGRESQL.sql",
}


def init_schema() -> None:
    """Import MISP's install baseline on an empty database."""
    if query_ok("SELECT 1 FROM attributes LIMIT 1"):
        log.info("database already initialized")
        return
    path = SCHEMA_FILES[engine()]
    log.info("importing MISP database schema from %s", path)
    with open(path) as f:
        sql = f.read()
    # The PostgreSQL baseline is one transaction (BEGIN ... COMMIT in the
    # file); the statements run under one connection with commit at the end.
    conn = _connect(autocommit=False)
    try:
        with _cursor(conn) as cur:
            for statement in split_sql_statements(sql):
                if statement.upper() in ("BEGIN", "COMMIT", "START TRANSACTION"):
                    continue
                cur.execute(statement)
        conn.commit()
    finally:
        conn.close()


def split_sql_statements(sql: str) -> list[str]:
    """Split a SQL dump into statements.

    A ';' inside a quoted string or a comment does not end a statement.
    '--' and '#' comments and plain block comments are dropped; MySQL
    conditional comments (/*!...*/) are statements and kept.
    """
    statements = []
    buf = []
    i = 0
    n = len(sql)
    quote = None
    while i < n:
        c = sql[i]
        if quote:
            buf.append(c)
            if c == "\\" and i + 1 < n:
                buf.append(sql[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            quote = c
            buf.append(c)
            i += 1
            continue
        if sql.startswith("--", i) or c == "#":
            end = sql.find("\n", i)
            i = n if end == -1 else end
            continue
        if sql.startswith("/*", i) and not sql.startswith("/*!", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if c == ";":
            text = "".join(buf).strip()
            if text:
                statements.append(text)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    text = "".join(buf).strip()
    if text:
        statements.append(text)
    return statements


# -- Configure lock ----------------------------------------------------------
# MySQL releases a named lock when its connection closes, and PostgreSQL a
# session advisory lock likewise, so the lock connection stays open from
# acquire until release.
_lock_conn = None
LOCK_NAME = "misp_configure"


def acquire_config_lock(timeout: int = 300) -> None:
    """Acquire the advisory lock for configuration and hold its connection."""
    global _lock_conn
    log.info("acquiring configuration lock")
    conn = _connect()
    try:
        with _cursor(conn) as cur:
            if is_postgres():
                deadline = time.monotonic() + timeout
                while True:
                    cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_NAME,))
                    if cur.fetchone()[0]:
                        result = 1
                        break
                    if time.monotonic() > deadline:
                        result = 0
                        break
                    time.sleep(2)
            else:
                cur.execute("SELECT GET_LOCK(%s, %s);", (LOCK_NAME, timeout))
                row = cur.fetchone()
                result = row[0] if row else None
    except Exception as e:
        conn.close()
        log.error("could not acquire configuration lock: %s", e)
        sys.exit(1)
    if result != 1:
        conn.close()
        log.error("could not acquire configuration lock (result: %s)", result)
        sys.exit(1)
    _lock_conn = conn


def release_config_lock() -> None:
    """Release the advisory lock and close its connection."""
    global _lock_conn
    if _lock_conn is None:
        return
    try:
        with _cursor(_lock_conn) as cur:
            if is_postgres():
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))
            else:
                cur.execute("SELECT RELEASE_LOCK(%s);", (LOCK_NAME,))
            _fetch_rows(cur)
    except Exception as e:
        log.warning("could not release configuration lock: %s", e)
    finally:
        _lock_conn.close()
        _lock_conn = None
