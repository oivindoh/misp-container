"""MISP Prometheus metrics exporter.

Collects operational metrics from the MISP database, the background job
queues in Redis and remote server connectivity checks, exposing them in
Prometheus exposition format.
"""

from __future__ import annotations

import base64
import http.client
import ssl
import threading
import time
import urllib.parse
from pathlib import Path

import redis
from asn1crypto import x509

from . import WORKER_QUEUES, db, exporter
from .config import SETTING_PREFIX, repository_settings
from .env import env
from .log import get as getlog, setup as setup_logging

log = getlog("metrics")

# The partner checks run in a thread of their own on this cadence; a scrape
# reads the last result, so a partner that drops packets never holds a scrape
NET_INTERVAL = 300
# The misp-certs Secret: the sync servers' pinned and client certificates, by the
# file names MISP keeps in servers.cert_file and servers.client_cert_file
CERTS = Path("/etc/misp-docker/certs")


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


def _connect():
    """A connection to MISP's database; the tests replace it."""
    return db._connect()


# ---------------------------------------------------------------------------
# Background job queues (Redis)
# ---------------------------------------------------------------------------

# MISP's key for a job a worker runs: <namespace>:running:<queue>:<job id>
RUNNING_PREFIX = "running"


def _job_redis_settings() -> dict[str, str]:
    """The Redis connection MISP uses for background jobs, from the env the chart renders.

    The password is the job backend's own when a container has it, else MISP_REDIS_PASSWORD,
    which the chart derives it from.
    """
    fallback = {"host": env("MISP_REDIS_HOST"), "port": env("MISP_REDIS_PORT"),
                "password": env("MISP_REDIS_PASSWORD")}
    return {key: env(f"{SETTING_PREFIX}SimpleBackgroundJobs__redis_{key}") or fallback.get(key, "")
            for key in ("host", "port", "password", "database", "namespace")}


def _collect_queue_metrics() -> str:
    """Jobs waiting in or running from each queue, read where MISP keeps them.

    MISP's jobs table is no queue: a row has status 0 while its job waits and
    runs, and 3 or 4 once it ends. The live state is in Redis: a list per queue
    for waiting jobs, and a running:<queue>:<id> key while a worker runs one.
    """
    cfg = _job_redis_settings()
    ns = cfg.get("namespace") or "background_jobs"
    host = cfg.get("host") or "redis"
    # MISP's phpredis connects over TLS to a host written tls://<name>
    client = redis.Redis(host=host.removeprefix("tls://"), port=int(cfg.get("port") or 6379),
                         password=cfg.get("password") or None, db=int(cfg.get("database") or 1),
                         ssl=host.startswith("tls://"), socket_timeout=3, socket_connect_timeout=3,
                         decode_responses=True)
    try:
        counts = {q: client.llen(f"{ns}:{q}") for q in WORKER_QUEUES}
        running_prefix = f"{ns}:{RUNNING_PREFIX}:"
        for key in client.scan_iter(match=f"{running_prefix}*", count=1000):
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


def _collect_db_metrics() -> tuple[str, int]:
    """Collect metrics from the MISP database.

    Returns (prometheus_text, errors): a collector that fails is logged and
    counted, and the other collectors still run. Fresh on every call --
    queries are cheap.
    """
    blocks: list[str] = []
    errors = 0

    try:
        conn = _connect()
    except Exception as e:
        log.warning("cannot connect to the database: %s", e)
        blocks.append(
            _metric("misp_up", "Whether the MISP database is reachable", "gauge", [({}, 0)])
        )
        return "\n\n".join(blocks), 1

    try:
        blocks.append(
            _metric("misp_up", "Whether the MISP database is reachable", "gauge", [({}, 1)])
        )

        with db.dict_cursor(conn) as cur:
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
                log.warning("instance info: %s", e)
                errors += 1
            # configure deletes the rows of the settings MISP takes from its env layer
            for name in ("MISP.uuid", "MISP.baseurl"):
                info[name] = env(SETTING_PREFIX + name.replace(".", "__")) or info.get(name, "")
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

            # Content counts: PostgreSQL's live-row estimates for the large tables,
            # exact counts for the small ones
            try:
                cur.execute(
                    "SELECT relname, GREATEST(n_live_tup, 0) AS live_rows FROM pg_stat_user_tables "
                    "WHERE relname IN ('events', 'attributes', 'shadow_attributes')"
                )
                approx = {r["relname"]: int(r["live_rows"] or 0) for r in cur.fetchall()}

                # Small tables: exact counts are cheap
                cur.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM organisations) AS orgs,
                        (SELECT COUNT(*) FROM organisations WHERE local = TRUE) AS orgs_local,
                        (SELECT COUNT(*) FROM users WHERE disabled = FALSE) AS users_active,
                        (SELECT COUNT(*) FROM users WHERE disabled = TRUE) AS users_disabled,
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
                log.warning("content counts: %s", e)
                errors += 1

            # Sync servers
            try:
                cur.execute(
                    "SELECT id, name, url, pull, push, "
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
                log.warning("servers: %s", e)
                errors += 1

            # -- Jobs ----------------------------------------------------------
            # MISP writes 0 when it creates a job and 3 or 4 when the job ends; 0
            # covers waiting, running and lost jobs alike. The queue depth comes
            # from Redis (_collect_queue_metrics).
            status_map = {0: "unfinished", 1: "queued", 2: "running", 3: "failed", 4: "completed"}
            try:
                cur.execute("SELECT worker, job_type, status, COUNT(*) AS cnt FROM jobs "
                            "GROUP BY worker, job_type, status")
                samples = [({"worker": r["worker"] or "unknown", "job_type": r["job_type"] or "unknown",
                             "status": status_map.get(r["status"], str(r["status"]))}, r["cnt"])
                           for r in cur.fetchall()]
                if samples:
                    blocks.append(_metric("misp_jobs",
                                          "Rows of MISP's jobs table by worker queue, type and status",
                                          "gauge", samples))
                # The last completed and the last failed sync job per server; the server
                # ID is the tail of job_input ("Server: 3")
                tail = "regexp_replace(j.job_input, '^.*: ', '')"
                cur.execute(
                    f"""
                    SELECT s.id, s.name, j.job_type, j.status,
                           EXTRACT(EPOCH FROM MAX(j.date_modified)) AS last
                    FROM jobs j
                    JOIN servers s
                      ON CASE WHEN {tail} ~ '^[0-9]+$' THEN {tail}::bigint END = s.id
                    WHERE j.job_type IN ('pull', 'push') AND j.status IN (3, 4)
                    GROUP BY s.id, s.name, j.job_type, j.status
                    """
                )
                last = [({"id": str(r["id"]), "name": r["name"] or "", "job_type": r["job_type"],
                          "status": status_map[r["status"]]}, int(r["last"]))
                        for r in cur.fetchall()]
                if last:
                    blocks.append(_metric("misp_server_job_last_timestamp_seconds",
                                          "Unix time of the last completed and the last failed pull and push "
                                          "per server", "gauge", last))
            except Exception as e:
                log.warning("jobs: %s", e)
                errors += 1

            # Tasks under MISP's Scheduled tasks page: no scheduler_worker
            # runs in this deployment, so an enabled one never runs
            try:
                cur.execute(
                    "SELECT type, COUNT(*) AS cnt FROM scheduled_tasks "
                    "WHERE enabled = TRUE GROUP BY type"
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
                log.warning("scheduled tasks: %s", e)
                errors += 1

            # Runs of the container jobs, from their system_settings rows (db.record_run)
            try:
                cur.execute("SELECT setting, value FROM system_settings WHERE setting LIKE %s",
                            (db.RUNS_PREFIX + "%",))
                records = db.parse_run_records(cur.fetchall())
                runs = [({"operation": op, "status": status}, int(r.get(status, 0)))
                        for op, r in sorted(records.items()) for status in ("success", "error")]
                last = [({"operation": op}, int(r["last_success"]))
                        for op, r in sorted(records.items()) if r.get("last_success")]
                if runs:
                    blocks.append(_metric("misp_container_job_runs_total",
                                          "Runs of the container jobs (configure, org-sync, migrate) by outcome",
                                          "counter", runs))
                if last:
                    blocks.append(_metric("misp_container_job_last_success_timestamp_seconds",
                                          "Unix timestamp of the last successful run per container job",
                                          "gauge", last))
            except Exception as e:
                log.warning("job runs: %s", e)
                errors += 1

            # Settings changed in MISP that the repository does not carry: the state of
            # MISP is defined in git, so each one should move there or be undone
            try:
                manual = db.manual_settings(repository_settings())
                blocks.append(_metric("misp_settings_manual",
                                      "Settings changed in MISP that the repository does not carry", "gauge",
                                      [({}, len(manual))]))
                if manual:
                    blocks.append(_metric("misp_setting_manual",
                                          "A setting changed in MISP that the repository does not carry", "gauge",
                                          [({"setting": m["setting"]}, 1) for m in manual]))
            except Exception as e:
                log.warning("manual settings: %s", e)
                errors += 1

    except Exception as e:
        log.warning("error collecting DB metrics: %s", e)
        errors += 1
    finally:
        conn.close()

    return "\n\n".join(blocks), errors


# ---------------------------------------------------------------------------
# Network checks (reachability + TLS cert expiry)
# ---------------------------------------------------------------------------


# MISP stores servers.authkey as this marker plus ciphertext once
# Security.encryption_key is set (EncryptedValue::ENCRYPTED_MAGIC)
ENCRYPTED_MAGIC = "\x1f\x1d"


def _setting(name: str) -> str:
    """A MISP setting from the env layer the chart renders."""
    return env(SETTING_PREFIX + name.replace(".", "__"))


def _partner_tls(server: dict) -> ssl.SSLContext:
    """The TLS checks MISP makes on this partner (SyncTool::setupHttpSocket)."""
    cert_file = server.get("cert_file") or ""
    ctx = ssl.create_default_context(cafile=str(CERTS / cert_file) if cert_file else _setting("MISP.ca_path") or None)
    if server.get("self_signed"):
        ctx.check_hostname = False
        if not cert_file:
            ctx.verify_mode = ssl.CERT_NONE
    if server.get("client_cert_file"):
        ctx.load_cert_chain(str(CERTS / server["client_cert_file"]))
    return ctx


def _proxy() -> dict | None:
    """MISP's HTTP proxy (Proxy.*); None without Proxy.host."""
    host = _setting("Proxy.host")
    if not host:
        return None
    headers = {}
    if _setting("Proxy.user"):
        token = base64.b64encode(f"{_setting('Proxy.user')}:{_setting('Proxy.password')}".encode()).decode()
        headers["Proxy-Authorization"] = f"Basic {token}"
    # 3128 is the port MISP's HttpSocket takes when Proxy.port is empty
    return {"host": host, "port": int(_setting("Proxy.port") or 3128), "headers": headers}


def _not_after(der: bytes | None) -> float | None:
    """The expiry of a DER certificate as a Unix timestamp; None when it does not parse."""
    try:
        return x509.Certificate.load(der).not_valid_after.timestamp()
    except Exception:
        return None


def _check_partner(server: dict, proxy: dict | None, timeout: int = 5) -> tuple[bool, float | None]:
    """GET /servers/getVersion on a partner, connected as MISP connects to it.

    Returns whether it answered, with our authkey unless MISP stores the key
    encrypted, and when its TLS certificate expires. An encrypted authkey cannot
    be sent, so for those any HTTP answer counts, 401 and 403 included.
    """
    authkey = server.get("authkey") or ""
    if isinstance(authkey, bytes):
        # The column is bytea; latin-1 keeps the marker bytes intact
        authkey = authkey.decode("latin-1")
    encrypted = authkey.startswith(ENCRYPTED_MAGIC)
    headers = {"Accept": "application/json"}
    if not encrypted:
        headers["Authorization"] = authkey

    url = urllib.parse.urlsplit(server["url"].rstrip("/"))
    https = url.scheme == "https"
    port = url.port or (443 if https else 80)
    path = f"{url.path}/servers/getVersion"
    via = proxy if proxy and not server.get("skip_proxy") else None
    host, connect_port = (via["host"], via["port"]) if via else (url.hostname, port)
    try:
        if https:
            conn = http.client.HTTPSConnection(host, connect_port, timeout=timeout, context=_partner_tls(server))
            if via:
                conn.set_tunnel(url.hostname, port, headers=via["headers"])
        else:
            conn = http.client.HTTPConnection(host, connect_port, timeout=timeout)
            if via:
                path = f"http://{url.hostname}:{port}{path}"
                headers.update(via["headers"])
    except Exception:
        return False, None
    try:
        conn.connect()
        # The certificate MISP sees, also when it checks none (self_signed without a pinned one)
        expiry = _not_after(conn.sock.getpeercert(binary_form=True)) if https else None
        conn.request("GET", path, headers=headers)
        status = conn.getresponse().status
    except Exception:
        return False, None
    finally:
        conn.close()
    return (200 <= status < 400 or (encrypted and status in (401, 403))), expiry


def _collect_network_metrics(servers: list[dict]) -> str:
    """Check reachability and TLS cert status of the sync servers given."""
    if not servers:
        return ""

    blocks: list[str] = []
    reachable_samples = []
    cert_samples = []
    proxy = _proxy()

    for s in servers:
        url = s.get("url", "")
        if not url:
            continue
        lbl = {"id": str(s["id"]), "name": s.get("name") or "", "url": url}
        ok, expiry = _check_partner(s, proxy)
        reachable_samples.append((lbl, 1 if ok else 0))
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

    return "\n\n".join(blocks)


# The last partner check block, written by the check thread and read by the scrape
_net_lock = threading.Lock()
_net_output = ""


def _partner_servers() -> list[dict]:
    conn = _connect()
    try:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT id, name, url, authkey, self_signed, cert_file, client_cert_file, skip_proxy "
                        "FROM servers")
            return list(cur.fetchall())
    finally:
        conn.close()


def refresh_network_metrics() -> None:
    """One round of the partner checks; the scrape serves the result until the next."""
    global _net_output
    try:
        output = _collect_network_metrics(_partner_servers())
    except Exception as e:
        log.warning("partner checks failed: %s", e)
        return
    with _net_lock:
        _net_output = output


def network_metrics() -> str:
    with _net_lock:
        return _net_output


def run_network_checks(interval: float = NET_INTERVAL) -> None:
    """Refresh the partner checks every interval seconds; the exporter runs this in a daemon thread."""
    while True:
        refresh_network_metrics()
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_all() -> str:
    """Collect all metrics, return Prometheus exposition format text."""
    start = time.monotonic()
    errors = 0

    try:
        db_output, errors = _collect_db_metrics()
    except Exception as e:
        log.error("DB metrics collection failed: %s", e)
        db_output = _metric("misp_up", "Whether the MISP database is reachable", "gauge", [({}, 0)])
        errors += 1

    try:
        queue_output = _collect_queue_metrics()
    except Exception as e:
        log.warning("cannot read the job queues from Redis: %s", e)
        queue_output = ""
        errors += 1

    net_output = network_metrics()

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


def main():
    """The metrics role: the exporter (misp_container.exporter)."""
    setup_logging("metrics")
    log.info("MISP metrics exporter starting")
    db.wait_for_db(retries=30, wait_seconds=5)

    # The partner checks take seconds per unreachable server; they run beside the
    # server, so a scrape and the probes never wait for them
    threading.Thread(target=run_network_checks, name="partner-checks", daemon=True).start()

    exporter.serve(collect_all)


if __name__ == "__main__":
    main()
