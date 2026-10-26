"""Database access for the entrypoints, on PostgreSQL.

DB_HOST, DB_PORT, DB_NAME, DB_USER and DB_PASSWORD describe the connection;
DB_SSLMODE and DB_SSLROOTCERT its TLS, in libpq's terms.
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
# libpq's sslmode values; allow, which tries plain text first, is left out
SSLMODES = ("disable", "prefer", "require", "verify-ca", "verify-full")


def settings() -> dict:
    """Connection settings from env."""
    e = envmod.env
    sslmode = e("DB_SSLMODE") or "prefer"
    if sslmode not in SSLMODES:
        raise ValueError(f"DB_SSLMODE={sslmode}: use one of {', '.join(SSLMODES)}")
    # libpq checks against the system's CAs only in verify-full (sslrootcert=system)
    if sslmode == "verify-ca" and not e("DB_SSLROOTCERT"):
        raise ValueError("DB_SSLMODE=verify-ca needs DB_SSLROOTCERT, the CA certificate of the server")
    return {
        "host": e("DB_HOST"),
        "port": int(e("DB_PORT") or "5432"),
        "user": e("DB_USER"),
        "password": e("DB_PASSWORD"),
        "database": e("DB_NAME"),
        "sslmode": sslmode,
        "sslrootcert": e("DB_SSLROOTCERT"),
    }


def ssl_context(sslmode: str, sslrootcert: str = ""):
    """pg8000's ssl_context for an sslmode.

    pg8000 takes False for no TLS, None for TLS when the server offers it and True
    for TLS or nothing, both without a check of the certificate.
    """
    if sslmode in ("disable", "prefer", "require"):
        return {"disable": False, "prefer": None, "require": True}[sslmode]
    import ssl
    context = ssl.create_default_context(cafile=sslrootcert or None)
    context.check_hostname = sslmode == "verify-full"
    return context


# -- Connections and queries -------------------------------------------------

def _connect(autocommit: bool = True):
    """A DB-API connection from the environment, over TLS as DB_SSLMODE says."""
    import pg8000.dbapi
    s = settings()
    kwargs = {"user": s["user"], "password": s["password"], "host": s["host"],
              "port": s["port"], "database": s["database"]}
    conn = pg8000.dbapi.connect(**kwargs, ssl_context=ssl_context(s["sslmode"], s["sslrootcert"]))
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


# The configure run's record of what it configured: the image version and the checksum of the
# chart's configuration (the pods' checksum/config annotation, MISP_CONFIG_CHECKSUM)
VERSION_ROW = "misp_docker.defaults_version"
CHECKSUM_ROW = "misp_docker.config_checksum"


def image_version() -> str:
    """The MISP version of this image, as the build recorded it; "" when unknown."""
    from . import DIST_VERSION_FILE
    try:
        return open(DIST_VERSION_FILE).read().strip()
    except OSError:
        return ""


def configured_for() -> dict[str, str]:
    """{VERSION_ROW, CHECKSUM_ROW: what this pod's configure run must have recorded}; empty values dropped."""
    wanted = {VERSION_ROW: image_version(), CHECKSUM_ROW: envmod.env("MISP_CONFIG_CHECKSUM")}
    return {row: value for row, value in wanted.items() if value}


def record_configured() -> None:
    """Record that a configure run finished for this image and this configuration."""
    for row, value in configured_for().items():
        set_system_setting(row, value)


def clear_configured() -> None:
    """Forget the last configure run: the pods wait for the next one."""
    execute("DELETE FROM system_settings WHERE setting IN (%s, %s)", (VERSION_ROW, CHECKSUM_ROW))


def wait_for_configure(retries: int = 120, wait_seconds: int = 3) -> None:
    """Wait for the configure run of this image and this configuration.

    A pod of a newer image or of a changed configuration waits for its own
    configure run, whatever tool applied the manifests and in whatever order.
    MISP.live is MISP's own maintenance switch: the wait ignores it.
    """
    wanted = configured_for()
    log.info("waiting for the configure run of %s", ", ".join(f"{k}={v}" for k, v in wanted.items()) or "anything")
    for i in range(retries, 0, -1):
        found = {}
        for row in dict_query("SELECT setting, value FROM system_settings WHERE setting IN (%s, %s)",
                              (VERSION_ROW, CHECKSUM_ROW)):
            try:
                found[row["setting"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                found[row["setting"]] = row["value"]
        if all(found.get(row) == value for row, value in wanted.items()) and (wanted or found):
            log.info("configured for %s", ", ".join(f"{k}={v}" for k, v in found.items()))
            return
        log.info("waiting: configured for %s (%d retries left)",
                 ", ".join(f"{k}={v}" for k, v in found.items()) or "nothing", i)
        time.sleep(wait_seconds)
    log.error("no configure run for %s within the timeout; is the configure Job running, or did it fail?",
              ", ".join(f"{k}={v}" for k, v in wanted.items()) or "this pod")
    sys.exit(1)


# Rows of system_settings that this image or MISP itself writes: no change by hand
OWN_PREFIX = "misp_docker."
OWN_SETTINGS = ("MISP.live", "MISP.host_org_id", "MISP.uuid")


def manual_settings(carried: set[str]) -> list[dict]:
    """The settings a person changed in MISP that the repository does not carry.

    [{"setting", "changed"}]: changed names who and when from MISP's log of the edit,
    or is empty once housekeeping deleted it, or for an edit through the console.
    """
    names = [row["setting"] for row in dict_query("SELECT setting FROM system_settings ORDER BY setting")
             if not row["setting"].startswith(OWN_PREFIX) and row["setting"] not in OWN_SETTINGS
             and row["setting"] not in carried]
    if not names:
        return []
    changed = {}
    for row in dict_query("SELECT change, email, created FROM logs WHERE action = 'serverSettingsEdit' "
                          "ORDER BY id DESC LIMIT 1000"):
        name = (row["change"] or "").split(" ", 1)[0]
        if name in names and name not in changed:
            changed[name] = f"by {row['email']} at {row['created']}"
    return [{"setting": name, "changed": changed.get(name, "")} for name in names]


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
