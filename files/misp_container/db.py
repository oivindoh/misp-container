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
            # MYSQL.sql contains multiple statements
            for statement in sql.split(";"):
                statement = statement.strip()
                if statement:
                    cur.execute(statement)
    finally:
        conn.close()


def acquire_config_lock() -> None:
    """Acquire a MySQL advisory lock for configuration."""
    log.info("acquiring configuration lock")
    result = query("SELECT GET_LOCK('misp_configure', 300);").strip()
    if result != "1":
        log.error("could not acquire configuration lock (result: %s)", result)
        sys.exit(1)


def release_config_lock() -> None:
    """Release the MySQL advisory lock."""
    query("SELECT RELEASE_LOCK('misp_configure');")
