"""MISP Prometheus metrics exporter.

Collects operational metrics from the MISP database, the background job
queues in Redis and remote server connectivity checks, exposing them in
Prometheus exposition format.
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from functools import lru_cache

from . import WORKER_QUEUES
from .env import env
from .log import get as getlog

log = getlog("metrics")

# Network check cache -- avoid hammering remote servers on every scrape
_NET_TTL = 300
_net_cache: dict = {"output": "", "ts": 0}


# ---------------------------------------------------------------------------
# Prometheus formatting
# ---------------------------------------------------------------------------


def _escape_label(v: str) -> str:
    """Escape a Prometheus label value."""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _metric(name: str, help_text: str, mtype: str, samples: list) -> str:
    """Format a metric block in Prometheus exposition format.

    samples: list of (labels_dict, value) tuples.
    """
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {mtype}"]
    for labels, value in samples:
        if labels:
            pairs = ",".join(
                f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items())
            )
            lines.append(f"{name}{{{pairs}}} {value}")
        else:
            lines.append(f"{name} {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Database connection (DictCursor for structured results)
# ---------------------------------------------------------------------------


def _connect():
    """A connection whose cursor returns dict rows on either engine."""
    from . import db
    return _DictConnection(db._connect())


class _DictConnection:
    """DB-API connection wrapper: cursors yield dicts keyed by column name."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _DictCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


class _DictCursor:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._cur.close()
        return False

    def execute(self, sql, params=None):
        return self._cur.execute(sql, params or ())

    def _names(self):
        return [d[0] for d in (self._cur.description or [])]

    def _row(self, row):
        return None if row is None else {n: v for n, v in zip(self._names(), row)}

    def fetchone(self):
        return self._row(self._cur.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()]


# ---------------------------------------------------------------------------
# Background job queues (Redis)
# ---------------------------------------------------------------------------

# MISP's key for a job a worker runs: <namespace>:running:<queue>:<job id>
RUNNING_PREFIX = "running"


class RedisError(Exception):
    pass


class _Redis:
    """The few Redis commands the exporter needs, over one socket (RESP2)."""

    def __init__(self, host: str, port, password: str = "", database=0, timeout: float = 3):
        self._sock = socket.create_connection((host, int(port)), timeout=timeout)
        self._reader = self._sock.makefile("rb")
        if password:
            self.call("AUTH", password)
        if int(database or 0):
            self.call("SELECT", database)

    def call(self, *args):
        parts = [b"*%d\r\n" % len(args)]
        for arg in args:
            data = str(arg).encode()
            parts.append(b"$%d\r\n%s\r\n" % (len(data), data))
        self._sock.sendall(b"".join(parts))
        return self._reply()

    def _reply(self):
        line = self._reader.readline()
        if not line.endswith(b"\r\n"):
            raise RedisError("connection closed")
        kind, rest = line[:1], line[1:-2]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RedisError(rest.decode(errors="replace"))
        if kind == b":":
            return int(rest)
        if kind == b"$":
            size = int(rest)
            if size < 0:
                return None
            return self._reader.read(size + 2)[:-2].decode(errors="replace")
        if kind == b"*":
            size = int(rest)
            return None if size < 0 else [self._reply() for _ in range(size)]
        raise RedisError(f"unexpected reply {line[:40]!r}")

    def scan(self, pattern: str) -> list[str]:
        keys, cursor = [], "0"
        while True:
            cursor, batch = self.call("SCAN", cursor, "MATCH", pattern, "COUNT", 1000)
            keys.extend(batch)
            if cursor == "0":
                return keys

    def close(self) -> None:
        self._reader.close()
        self._sock.close()


@lru_cache(maxsize=1)
def _job_redis_settings() -> dict[str, str]:
    """The Redis connection MISP uses for background jobs: env over settings.yaml."""
    from .config import load_settings_yaml
    wanted = {f"SimpleBackgroundJobs.redis_{k}": k
              for k in ("host", "port", "password", "database", "namespace")}
    found = {}
    for specs in load_settings_yaml().values():
        for spec in specs:
            if spec.name in wanted:
                found[wanted[spec.name]] = spec.effective_value
    return found


def _collect_queue_metrics() -> str:
    """Jobs waiting in or running from each queue, read where MISP keeps them.

    MISP's jobs table is no queue: a row has status 0 while its job waits and
    runs, and 3 or 4 once it ends. The live state is in Redis: a list per queue
    for waiting jobs, and a running:<queue>:<id> key while a worker runs one.
    """
    cfg = _job_redis_settings()
    ns = cfg.get("namespace") or "background_jobs"
    client = _Redis(cfg.get("host") or "redis", cfg.get("port") or 6379,
                    cfg.get("password", ""), cfg.get("database") or 1)
    try:
        counts = {q: int(client.call("LLEN", f"{ns}:{q}")) for q in WORKER_QUEUES}
        running_prefix = f"{ns}:{RUNNING_PREFIX}:"
        for key in client.scan(f"{running_prefix}*"):
            queue = key[len(running_prefix):].split(":", 1)[0]
            counts[queue] = counts.get(queue, 0) + 1
    finally:
        client.close()
    return _metric(
        "misp_jobs_queued",
        "Background jobs waiting or running by worker queue (Redis)",
        "gauge",
        [({"worker": q}, n) for q, n in sorted(counts.items())],
    )


# ---------------------------------------------------------------------------
# DB metrics collection
# ---------------------------------------------------------------------------


def _collect_db_metrics() -> tuple[str, list[dict]]:
    """Collect metrics from the MISP database.

    Returns (prometheus_text, server_list) where server_list is used
    by the network checker. Fresh on every call -- queries are cheap.
    """
    blocks: list[str] = []
    servers: list[dict] = []

    try:
        conn = _connect()
    except Exception as e:
        log.warning("cannot connect to MySQL: %s", e)
        blocks.append(
            _metric("misp_up", "Whether the MISP database is reachable", "gauge", [({}, 0)])
        )
        return "\n\n".join(blocks), []

    try:
        blocks.append(
            _metric("misp_up", "Whether the MISP database is reachable", "gauge", [({}, 1)])
        )

        with conn.cursor() as cur:
            # Instance info
            info = {}
            try:
                cur.execute(
                    "SELECT setting, value FROM system_settings "
                    "WHERE setting IN ('MISP.uuid', 'MISP.baseurl')"
                )
                for r in cur.fetchall():
                    v = r["value"]
                    if isinstance(v, bytes):
                        v = v.decode()
                    if v is not None:
                        v = str(v).strip('"')
                    info[r["setting"]] = v or ""
            except Exception as e:
                log.debug("instance info: %s", e)
            blocks.append(
                _metric(
                    "misp_instance_info",
                    "MISP instance metadata",
                    "gauge",
                    [
                        (
                            {
                                "uuid": info.get("MISP.uuid", ""),
                                "base_url": info.get("MISP.baseurl", ""),
                            },
                            1,
                        )
                    ],
                )
            )

            # Content counts -- use information_schema estimates for large
            # tables (attributes, shadow_attributes) to avoid full index scans.
            # Small tables (orgs, users, tags) use exact COUNT(*).
            try:
                from . import db as dbmod
                if dbmod.is_postgres():
                    cur.execute(
                        'SELECT relname AS "TABLE_NAME", GREATEST(n_live_tup, 0) AS "TABLE_ROWS" '
                        "FROM pg_stat_user_tables "
                        "WHERE relname IN ('events', 'attributes', 'shadow_attributes')"
                    )
                else:
                    cur.execute(
                        "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = %s "
                        "AND TABLE_NAME IN ('events', 'attributes', 'shadow_attributes')",
                        (env("DB_NAME"),),
                    )
                approx = {r["TABLE_NAME"]: int(r["TABLE_ROWS"] or 0) for r in cur.fetchall()}

                # Small tables: exact counts are cheap
                from . import db as dbmod
                t, f = dbmod.bool_lit(True), dbmod.bool_lit(False)
                cur.execute(
                    f"""
                    SELECT
                        (SELECT COUNT(*) FROM organisations) AS orgs,
                        (SELECT COUNT(*) FROM organisations WHERE local = {t}) AS orgs_local,
                        (SELECT COUNT(*) FROM users WHERE disabled = {f}) AS users_active,
                        (SELECT COUNT(*) FROM users WHERE disabled = {t}) AS users_disabled,
                        (SELECT COUNT(*) FROM sharing_groups) AS sharing_groups,
                        (SELECT COUNT(*) FROM tags) AS tags
                    """
                )
                c = cur.fetchone()
                blocks.append(_metric("misp_events", "Approximate total number of events", "gauge", [({}, approx.get("events", 0))]))
                blocks.append(_metric("misp_attributes", "Approximate total number of attributes", "gauge", [({}, approx.get("attributes", 0))]))
                blocks.append(_metric("misp_proposals", "Approximate pending proposals", "gauge", [({}, approx.get("shadow_attributes", 0))]))
                blocks.append(_metric("misp_organisations", "Total number of organisations", "gauge", [({}, c["orgs"])]))
                blocks.append(_metric("misp_organisations_local", "Number of local organisations", "gauge", [({}, c["orgs_local"])]))
                blocks.append(
                    _metric(
                        "misp_users",
                        "Number of users by status",
                        "gauge",
                        [
                            ({"status": "active"}, c["users_active"]),
                            ({"status": "disabled"}, c["users_disabled"]),
                        ],
                    )
                )
                blocks.append(_metric("misp_sharing_groups", "Total number of sharing groups", "gauge", [({}, c["sharing_groups"])]))
                blocks.append(_metric("misp_tags", "Total number of tags", "gauge", [({}, c["tags"])]))
            except Exception as e:
                log.debug("content counts: %s", e)

            # Sync servers
            try:
                cur.execute(
                    "SELECT id, name, url, authkey, pull, push, "
                    "lastpulledid, lastpushedid FROM servers"
                )
                servers = list(cur.fetchall())

                if servers:
                    info_samples = []
                    pull_samples = []
                    push_samples = []
                    last_pull = []
                    last_push = []

                    for s in servers:
                        lbl = {"id": str(s["id"]), "name": s["name"] or "", "url": s["url"] or ""}
                        id_name = {"id": str(s["id"]), "name": s["name"] or ""}
                        info_samples.append((lbl, 1))
                        pull_samples.append((id_name, int(bool(s["pull"]))))
                        push_samples.append((id_name, int(bool(s["push"]))))
                        if s["lastpulledid"]:
                            last_pull.append((id_name, s["lastpulledid"]))
                        if s["lastpushedid"]:
                            last_push.append((id_name, s["lastpushedid"]))

                    blocks.append(_metric("misp_server_info", "Configured MISP sync servers", "gauge", info_samples))
                    blocks.append(_metric("misp_server_pull_enabled", "Whether pull is enabled for server", "gauge", pull_samples))
                    blocks.append(_metric("misp_server_push_enabled", "Whether push is enabled for server", "gauge", push_samples))
                    if last_pull:
                        blocks.append(_metric("misp_server_last_pull_event_id", "Last event ID pulled from server", "gauge", last_pull))
                    if last_push:
                        blocks.append(_metric("misp_server_last_push_event_id", "Last event ID pushed to server", "gauge", last_push))
            except Exception as e:
                log.debug("servers: %s", e)

            # -- Jobs ----------------------------------------------------------
            # Job totals are counters (monotonic until MISP prunes the table --
            # Prometheus handles counter resets via increase()/rate()). The
            # queue depth comes from Redis (_collect_queue_metrics).
            try:
                # MISP writes 0 when it creates a job and 3 or 4 when the job
                # ends; 0 covers waiting, running and lost jobs alike
                status_map = {0: "unfinished", 1: "queued", 2: "running", 3: "failed", 4: "completed"}

                # All jobs in one query -- LEFT JOIN to servers for pull/push,
                # split into server vs non-server in Python.
                from . import db as dbmod
                if dbmod.is_postgres():
                    server_id_expr = "NULLIF(regexp_replace(j.job_input, '^.*: ', ''), '')::bigint"
                else:
                    server_id_expr = "CAST(SUBSTRING_INDEX(j.job_input, ': ', -1) AS UNSIGNED)"
                cur.execute(
                    f"""
                    SELECT j.worker, j.job_type, j.status,
                           s.id AS server_id, s.name AS server_name,
                           COUNT(*) AS cnt
                    FROM jobs j
                    LEFT JOIN servers s
                      ON j.job_type IN ('pull', 'push')
                     AND {server_id_expr} = s.id
                    GROUP BY j.worker, j.job_type, j.status, s.id, s.name
                    """
                )
                all_jobs = cur.fetchall()

                server_samples = []
                other_samples = []
                for r in all_jobs:
                    st = status_map.get(r["status"], str(r["status"]))
                    if r["server_id"] is not None:
                        server_samples.append(
                            (
                                {
                                    "id": str(r["server_id"]),
                                    "name": r["server_name"] or "",
                                    "job_type": r["job_type"],
                                    "status": st,
                                },
                                r["cnt"],
                            )
                        )
                    else:
                        other_samples.append(
                            (
                                {
                                    "worker": r["worker"] or "unknown",
                                    "job_type": r["job_type"] or "unknown",
                                    "status": st,
                                },
                                r["cnt"],
                            )
                        )

                if server_samples:
                    blocks.append(
                        _metric(
                            "misp_server_jobs_total",
                            "Sync jobs per server by type and status",
                            "counter",
                            server_samples,
                        )
                    )
                if other_samples:
                    blocks.append(
                        _metric(
                            "misp_jobs_total",
                            "Background jobs by worker queue, type, and status",
                            "counter",
                            other_samples,
                        )
                    )
            except Exception as e:
                log.debug("jobs: %s", e)

            # Tasks under MISP's Scheduled tasks page: no scheduler_worker
            # runs in this deployment, so an enabled one never runs
            try:
                from . import db as dbmod
                cur.execute(
                    "SELECT type, COUNT(*) AS cnt FROM scheduled_tasks "
                    f"WHERE enabled = {dbmod.bool_lit(True)} GROUP BY type"
                )
                rows = cur.fetchall()
                blocks.append(
                    _metric(
                        "misp_scheduled_tasks_enabled",
                        "Tasks enabled under MISP's Scheduled tasks, which nothing runs here",
                        "gauge",
                        [({"type": r["type"]}, r["cnt"]) for r in rows] if rows else [({}, 0)],
                    )
                )
            except Exception as e:
                log.debug("scheduled tasks: %s", e)

            # Sync container log (our custom table)
            try:
                from . import db as dbmod
                cur.execute(
                    "SELECT operation, status, "
                    "  COUNT(*) AS runs, "
                    "  MAX(timestamp) AS last_run, "
                    "  AVG(duration_seconds) AS avg_duration "
                    "FROM misp_container_sync_log "
                    f"WHERE timestamp > {dbmod.ago(24, 'HOUR')} "
                    "GROUP BY operation, status"
                )
                rows = cur.fetchall()
                if rows:
                    run_samples = []
                    for r in rows:
                        lbl = {"operation": r["operation"], "status": r["status"]}
                        run_samples.append((lbl, r["runs"]))
                    blocks.append(
                        _metric(
                            "misp_sync_runs_24h",
                            "Container runs (org-sync, configure) in the last 24 hours by outcome",
                            "gauge",
                            run_samples,
                        )
                    )

                # Last successful run per operation (org-sync, configure)
                cur.execute(
                    f"SELECT operation, {dbmod.epoch('MAX(timestamp)')} AS ts "
                    "FROM misp_container_sync_log WHERE status = 'success' GROUP BY operation"
                )
                last = [({"operation": r["operation"]}, int(r["ts"])) for r in cur.fetchall() if r.get("ts")]
                if last:
                    blocks.append(
                        _metric(
                            "misp_sync_last_success_timestamp_seconds",
                            "Unix timestamp of the last successful run per operation",
                            "gauge",
                            last,
                        )
                    )
            except Exception:
                # Table may not exist yet (sync hasn't run)
                pass

    except Exception as e:
        log.warning("error collecting DB metrics: %s", e)
    finally:
        conn.close()

    return "\n\n".join(blocks), servers


# ---------------------------------------------------------------------------
# Network checks (reachability + TLS cert expiry)
# ---------------------------------------------------------------------------


def _check_tls_cert(hostname: str, port: int = 443, timeout: int = 5) -> float | None:
    """Return the TLS cert expiry as a Unix timestamp, or None on failure."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                return ssl.cert_time_to_seconds(cert["notAfter"])
    except Exception:
        return None


# MISP stores servers.authkey as this marker plus ciphertext once
# Security.encryption_key is set (EncryptedValue::ENCRYPTED_MAGIC)
ENCRYPTED_MAGIC = "\x1f\x1d"


def _check_server_auth(url: str, authkey: str, timeout: int = 5) -> bool:
    """Check that a remote MISP server answers, with our auth when we have it.

    An encrypted authkey cannot be sent, so for those any HTTP answer counts
    as reachable, including 401 and 403.
    """
    if isinstance(authkey, bytes):
        # pymysql returns the column as bytes; latin-1 keeps the marker bytes intact
        authkey = authkey.decode("latin-1")
    encrypted = authkey.startswith(ENCRYPTED_MAGIC)
    headers = {"Accept": "application/json"}
    if not encrypted:
        headers["Authorization"] = authkey
    try:
        target = f"{url.rstrip('/')}/servers/getVersion"
        req = urllib.request.Request(target, headers=headers)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as e:
        return encrypted and e.code in (401, 403)
    except Exception:
        return False


def _collect_network_metrics(servers: list[dict]) -> str:
    """Check reachability and TLS cert status of configured sync servers."""
    now = time.monotonic()
    if now - _net_cache["ts"] < _NET_TTL:
        return _net_cache["output"]

    if not servers:
        _net_cache.update(output="", ts=now)
        return ""

    blocks: list[str] = []
    reachable_samples = []
    cert_samples = []

    for s in servers:
        url = s.get("url", "")
        authkey = s.get("authkey", "")
        if not url:
            continue

        lbl = {"id": str(s["id"]), "name": s.get("name") or "", "url": url}

        # Auth check (HEAD with authkey)
        ok = _check_server_auth(url, authkey)
        reachable_samples.append((lbl, 1 if ok else 0))

        # TLS cert expiry
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "https":
            port = parsed.port or 443
            expiry = _check_tls_cert(parsed.hostname, port)
            if expiry is not None:
                cert_samples.append((lbl, int(expiry)))

    if reachable_samples:
        blocks.append(
            _metric(
                "misp_server_reachable",
                "Whether the remote MISP server is reachable (auth verified unless the key is stored encrypted)",
                "gauge",
                reachable_samples,
            )
        )
    if cert_samples:
        blocks.append(
            _metric(
                "misp_server_tls_expiry_timestamp_seconds",
                "Unix timestamp when the remote server TLS certificate expires",
                "gauge",
                cert_samples,
            )
        )

    out = "\n\n".join(blocks)
    _net_cache.update(output=out, ts=now)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_all() -> str:
    """Collect all metrics, return Prometheus exposition format text."""
    start = time.monotonic()
    errors = 0

    try:
        db_output, servers = _collect_db_metrics()
    except Exception as e:
        log.error("DB metrics collection failed: %s", e)
        db_output = _metric("misp_up", "Whether the MISP database is reachable", "gauge", [({}, 0)])
        servers = []
        errors += 1

    try:
        queue_output = _collect_queue_metrics()
    except Exception as e:
        log.warning("cannot read the job queues from Redis: %s", e)
        queue_output = ""
        errors += 1

    try:
        net_output = _collect_network_metrics(servers)
    except Exception as e:
        log.error("network metrics collection failed: %s", e)
        net_output = ""
        errors += 1

    duration = time.monotonic() - start

    meta = "\n\n".join(
        [
            _metric(
                "misp_scrape_duration_seconds",
                "Time spent collecting metrics",
                "gauge",
                [({}, f"{duration:.3f}")],
            ),
            _metric(
                "misp_scrape_errors",
                "Number of errors during metrics collection",
                "gauge",
                [({}, errors)],
            ),
        ]
    )

    parts = [p for p in (db_output, queue_output, net_output, meta) if p]
    return "\n\n".join(parts) + "\n"


def init_sync_log_table() -> None:
    """Create the sync log table if it doesn't exist.

    Called by the sync entrypoint before logging results.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            from . import db as dbmod
            if dbmod.is_postgres():
                ddl = """
                CREATE TABLE IF NOT EXISTS misp_container_sync_log (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    operation VARCHAR(64) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    summary TEXT,
                    duration_seconds FLOAT,
                    error_message TEXT
                )
                """
            else:
                ddl = """
                CREATE TABLE IF NOT EXISTS misp_container_sync_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    operation VARCHAR(64) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    summary TEXT,
                    duration_seconds FLOAT,
                    error_message TEXT
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            cur.execute(ddl)
            # Prune entries older than 30 days
            cur.execute(f"DELETE FROM misp_container_sync_log WHERE timestamp < {dbmod.ago(30, 'DAY')}")
        conn.commit()
    finally:
        conn.close()


def log_sync_result(
    operation: str,
    status: str,
    summary: str = "",
    duration_seconds: float = 0,
    error_message: str = "",
) -> None:
    """Write a sync operation result to the log table."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO misp_container_sync_log "
                "(operation, status, summary, duration_seconds, error_message) "
                "VALUES (%s, %s, %s, %s, %s)",
                (operation, status, summary, duration_seconds, error_message),
            )
        conn.commit()
    finally:
        conn.close()
