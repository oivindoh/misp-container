"""Database access for the entrypoints, on PostgreSQL.

DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD and DB_TLS describe the
connection.
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import closing

from . import env as envmod
from .log import get as getlog

log = getlog("db")

# The current time as UNIX seconds, as MISP's integer timestamp columns hold it
NOW_EPOCH = "EXTRACT(EPOCH FROM NOW())::bigint"
SCHEMA_FILE = "/var/www/MISP/INSTALL/POSTGRESQL.sql"
# The prefix of the rows that count the runs of each container job (record_run)
RUNS_PREFIX = "misp_docker.runs."


def settings() -> dict:
    """Connection settings from env."""
    e = envmod.env
    return {
        "host": e("DB_HOST"),
        "port": int(e("DB_PORT") or "5432"),
        "user": e("DB_USER"),
        "password": e("DB_PASSWORD"),
        "database": e("DB_NAME"),
        "tls": e("DB_TLS") == "true",
    }


# -- Connections and queries -------------------------------------------------

def _connect(autocommit: bool = True):
    """A DB-API connection from the environment."""
    import pg8000.dbapi
    s = settings()
    kwargs = {"user": s["user"], "password": s["password"], "host": s["host"],
              "port": s["port"], "database": s["database"]}
    if s["tls"]:
        import ssl
        kwargs["ssl_context"] = ssl.create_default_context()
    conn = pg8000.dbapi.connect(**kwargs)
    conn.autocommit = autocommit
    return conn


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


def has_column(table: str, column: str) -> bool:
    """Whether a table of the database DB_* points at has this column."""
    rows = dict_query("SELECT 1 AS present FROM information_schema.columns "
                      "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                      (table, column))
    return bool(rows)


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
    log.info("waiting for PostgreSQL at %s:%s", s["host"], s["port"])
    for i in range(retries, 0, -1):
        if query_ok("SELECT 1"):
            log.info("database is ready")
            return
        log.info("waiting for database (%d retries left)", i)
        time.sleep(wait_seconds)
    log.error("could not connect to PostgreSQL at %s:%s", s["host"], s["port"])
    sys.exit(1)


def set_system_setting(name: str, value) -> None:
    """Write a row of system_settings, the value JSON-encoded as MISP stores it."""
    # The value column is bytea: bytes pass as they are, a string would be parsed for escapes
    execute("INSERT INTO system_settings (setting, value) VALUES (%s, %s) "
            "ON CONFLICT (setting) DO UPDATE SET value = EXCLUDED.value",
            (name, json.dumps(value).encode()))


def get_system_setting(name: str):
    """The JSON-decoded value of a row of system_settings; None when the row is missing."""
    rows = dict_query("SELECT value FROM system_settings WHERE setting = %s", (name,))
    if not rows:
        return None
    try:
        return json.loads(rows[0]["value"])
    except (TypeError, ValueError):
        return rows[0]["value"]


def record_run(operation: str, ok: bool) -> None:
    """Count a run of a container job in its system_settings row, for the metrics exporter.

    The row holds a count per status and the time of the last run per status.
    Two runs that end at once can lose one count; the count never goes back.
    """
    name = RUNS_PREFIX + operation
    state = get_system_setting(name)
    if not isinstance(state, dict):
        state = {}
    status = "success" if ok else "error"
    state[status] = int(state.get(status, 0)) + 1
    state[f"last_{status}"] = int(time.time())
    set_system_setting(name, state)


def parse_run_records(rows) -> dict[str, dict]:
    """{operation: its record} from system_settings rows (setting, value) of record_run."""
    out = {}
    for row in rows:
        value = row["value"]
        if isinstance(value, (bytes, bytearray, memoryview)):
            value = bytes(value).decode()
        try:
            state = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(state, dict):
            out[row["setting"][len(RUNS_PREFIX):]] = state
    return out


def run_records() -> dict[str, dict]:
    """{operation: its record} for every container job that recorded a run."""
    return parse_run_records(dict_query("SELECT setting, value FROM system_settings WHERE setting LIKE %s",
                                        (RUNS_PREFIX + "%",)))


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
    """Import MISP's install baseline on an empty database."""
    if query_ok("SELECT 1 FROM attributes LIMIT 1"):
        log.info("database already initialized")
        return
    log.info("importing MISP database schema from %s", SCHEMA_FILE)
    with open(SCHEMA_FILE) as f:
        sql = f.read()
    # The baseline is one transaction (BEGIN ... COMMIT in the file); the
    # statements run under one connection with commit at the end.
    conn = _connect(autocommit=False)
    try:
        with _cursor(conn) as cur:
            for statement in split_sql_statements(sql):
                if statement.upper() in ("BEGIN", "COMMIT"):
                    continue
                cur.execute(statement)
        conn.commit()
    finally:
        conn.close()


def split_sql_statements(sql: str) -> list[str]:
    """Split a SQL dump into statements.

    A ';' inside a quoted string or a comment does not end a statement.
    '--' comments and block comments are dropped.
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
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end
            continue
        if sql.startswith("/*", i):
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
# PostgreSQL releases a session advisory lock when its connection closes, so
# the lock connection stays open from acquire until release.
_lock_conn = None
LOCK_NAME = "misp_configure"


def acquire_config_lock(timeout: int = 300) -> None:
    """Acquire the advisory lock for configuration and hold its connection."""
    global _lock_conn
    log.info("acquiring configuration lock")
    conn = _connect()
    try:
        with _cursor(conn) as cur:
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
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))
            _fetch_rows(cur)
    except Exception as e:
        log.warning("could not release configuration lock: %s", e)
    finally:
        _lock_conn.close()
        _lock_conn = None
