"""MySQL database operations via pymysql (pure Python, no CLI dependency)."""

from __future__ import annotations

import sys
import time

from . import env as envmod
from .log import get as getlog

log = getlog("db")


def _connect():
    """Create a pymysql connection from environment variables."""
    import pymysql
    import pymysql.cursors

    e = envmod.env
    kwargs = {
        "host": e("MYSQL_HOST"),
        "port": int(e("MYSQL_PORT")),
        "user": e("MYSQL_USER"),
        "password": e("MYSQL_PASSWORD"),
        "database": e("MYSQL_DATABASE"),
        "autocommit": True,
        "charset": "utf8mb4",
    }
    if e("MYSQL_TLS") == "true":
        import ssl
        kwargs["ssl"] = {"ssl": ssl.create_default_context()}
    return pymysql.connect(**kwargs)


def query(sql: str, *, check: bool = False) -> str:
    """Run a SQL query. Returns the first column of the first row as string."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            if not rows:
                return ""
            # Format like mysql CLI -N: tab-separated columns, newline-separated rows
            lines = []
            for row in rows:
                cols = []
                for val in row:
                    if val is None:
                        cols.append("NULL")
                    elif isinstance(val, bytes):
                        cols.append(val.decode())
                    else:
                        cols.append(str(val))
                lines.append("\t".join(cols))
            return "\n".join(lines)
    except Exception as e:
        if check:
            raise RuntimeError(f"MySQL query failed: {e}") from e
        return ""
    finally:
        conn.close()


def query_ok(sql: str) -> bool:
    """Run a SQL query, return True if it succeeded."""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            return True
        finally:
            conn.close()
    except Exception:
        return False


def wait_for_mysql(retries: int = 100, wait_seconds: int = 5) -> None:
    """Wait for MySQL to become available."""
    host = envmod.env("MYSQL_HOST")
    port = envmod.env("MYSQL_PORT")
    log.info("waiting for MySQL at %s:%s", host, port)
    for i in range(retries, 0, -1):
        if query_ok("SHOW STATUS"):
            log.info("MySQL is ready")
            return
        log.info("waiting for database (%d retries left)", i)
        time.sleep(wait_seconds)
    log.error("could not connect to MySQL at %s:%s", host, port)
    sys.exit(1)


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


def init_schema() -> None:
    """Import MISP database schema if not already initialized."""
    if query_ok("DESCRIBE attributes"):
        log.info("database already initialized")
        return
    log.info("importing MISP database schema")
    with open("/var/www/MISP/INSTALL/MYSQL.sql") as f:
        sql = f.read()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            for statement in split_sql_statements(sql):
                cur.execute(statement)
    finally:
        conn.close()


def split_sql_statements(sql: str) -> list[str]:
    """Split a MySQL dump into statements.

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


# MySQL releases a named lock when its connection closes, so the lock
# connection stays open from acquire until release.
_lock_conn = None


def acquire_config_lock(timeout: int = 300) -> None:
    """Acquire the MySQL advisory lock for configuration and hold its connection."""
    global _lock_conn
    log.info("acquiring configuration lock")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK('misp_configure', %s);", (timeout,))
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
    """Release the MySQL advisory lock and close its connection."""
    global _lock_conn
    if _lock_conn is None:
        return
    try:
        with _lock_conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK('misp_configure');")
    except Exception as e:
        log.warning("could not release configuration lock: %s", e)
    finally:
        _lock_conn.close()
        _lock_conn = None
