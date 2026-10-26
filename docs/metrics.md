# Prometheus Metrics

## TL;DR

The metrics Deployment (`python3 -m misp_container.metrics` in the main image) exposes Prometheus metrics
on port 9191 from the database, from MISP's job queues in Redis and from probes of the
configured sync servers: instance health, content counts, server sync state and certificate
expiry, job queues, and the runs of the configure, org sync and migrate Jobs.
Scrape `/metrics`; `/healthz` and `/ready` answer 200.

## Endpoints

| Path | Description |
|------|-------------|
| `/metrics` | Prometheus metrics |
| `/healthz` | Liveness probe (returns 200) |
| `/ready` | Readiness probe (returns 200) |

## Metrics

**Instance health:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_up` | gauge | Whether the MISP database is reachable (1/0) |
| `misp_instance_info` | gauge | Instance metadata (uuid, base_url labels) |

**Content counts:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_events` | gauge | Approximate total events (PostgreSQL's row estimate) |
| `misp_attributes` | gauge | Approximate total attributes (PostgreSQL's row estimate) |
| `misp_proposals` | gauge | Approximate pending proposals |
| `misp_organisations` | gauge | Total organisations |
| `misp_organisations_local` | gauge | Local organisations |
| `misp_users{status}` | gauge | Users by active/disabled |
| `misp_sharing_groups` | gauge | Total sharing groups |
| `misp_tags` | gauge | Total tags |

**Server sync:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_server_info{id,name,url}` | gauge | Configured sync servers |
| `misp_server_pull_enabled{id,name}` | gauge | Pull enabled per server |
| `misp_server_push_enabled{id,name}` | gauge | Push enabled per server |
| `misp_server_last_pull_event_id{id,name}` | gauge | Last pulled event ID |
| `misp_server_last_push_event_id{id,name}` | gauge | Last pushed event ID |
| `misp_server_reachable{id,name,url}` | gauge | Whether the partner answers, connected as MISP connects to it, and with the authkey when MISP stores it in clear (checked every 5 minutes). With `Security.encryption_key` set, MISP stores authkeys encrypted and any HTTP answer counts as reachable |
| `misp_server_tls_expiry_timestamp_seconds{id,name,url}` | gauge | TLS cert expiry as unix timestamp |

**Background jobs:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_jobs_queued{worker}` | gauge | Jobs waiting or running per worker queue, read from Redis. Absent when Redis cannot be read |
| `misp_jobs{worker,job_type,status}` | gauge | Rows of MISP's jobs table. Housekeeping deletes old rows, so a count can fall |
| `misp_server_job_last_timestamp_seconds{id,name,job_type,status}` | gauge | Unix time of the last `completed` and the last `failed` pull and push per server |
| `misp_scheduled_tasks_enabled{type}` | gauge | Tasks enabled under MISP's Scheduled tasks page. No scheduler runs in this deployment, so these never run; the task runner does the periodic work |

The `status` label of `misp_jobs` is `completed`, `failed` or `unfinished`. MISP keeps a
job at `unfinished` while it waits and runs; a job whose worker died stays `unfinished`.

**Container jobs:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_container_job_runs_total{operation,status}` | counter | Runs of `configure`, `org-sync` and `migrate`, by `success` or `error` |
| `misp_container_job_last_success_timestamp_seconds{operation}` | gauge | Unix time of the last successful run per job |

**Settings outside git:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_settings_manual` | gauge | Settings changed in MISP that the repository does not carry ([configuration.md](configuration.md#settings)) |
| `misp_setting_manual{setting}` | gauge | 1 for each such setting |

**Self-monitoring:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_scrape_duration_seconds` | gauge | Collection time |
| `misp_scrape_errors` | gauge | Errors during collection |

## How the exporter reads

- **The database on every scrape**: no cache. `events`, `attributes` and `shadow_attributes` come from PostgreSQL's row estimate (`n_live_tup` of `pg_stat_user_tables`), the small tables from exact counts.
- **Partner checks beside the scrape**: a thread of the exporter probes every sync server (auth and TLS certificate) every 5 minutes; a scrape serves the last result, so an unreachable partner never delays a scrape or a probe of the exporter.
- **Partners as MISP connects to them**: a check takes the server's `self_signed`, its pinned and client certificates (`cert_file` and `client_cert_file`, from the `misp-certs` Secret), `MISP.ca_path`, and MISP's `Proxy.*` settings unless the server has `skip_proxy`.
- **Queue depth from Redis**: waiting jobs are the queue lists (`LLEN <namespace>:<queue>`) and running jobs the `<namespace>:running:<queue>:<id>` keys, in the database and namespace of `SimpleBackgroundJobs.redis_*`. A failed Redis read counts in `misp_scrape_errors`.
- **Jobs from the jobs table**: `misp_jobs` counts its rows. The last-job gauges take `date_modified` of the pull and push jobs that ended, by the server their `job_input` names.
- **Container job runs from `system_settings`**: each run of the configure, org sync and migrate Jobs adds one to its count in the row `misp_docker.runs.<operation>` of `system_settings`, and records the time.

## Example alerts

```yaml
# Server unreachable for 10 minutes
- alert: MISPServerUnreachable
  expr: misp_server_reachable == 0
  for: 10m

# TLS cert expires within 14 days
- alert: MISPServerCertExpiringSoon
  expr: misp_server_tls_expiry_timestamp_seconds - time() < 14 * 24 * 3600

# The last pull from a server failed
- alert: MISPPullFailing
  expr: |
    misp_server_job_last_timestamp_seconds{job_type="pull",status="failed"}
      > ignoring(status) misp_server_job_last_timestamp_seconds{job_type="pull",status="completed"}
    or (misp_server_job_last_timestamp_seconds{job_type="pull",status="failed"}
      unless ignoring(status) misp_server_job_last_timestamp_seconds{job_type="pull",status="completed"})

# Job queue backing up
- alert: MISPJobQueueBacklog
  expr: misp_jobs_queued > 50
  for: 5m

# A task enabled under MISP's Scheduled tasks never runs here
- alert: MISPScheduledTaskEnabled
  expr: misp_scheduled_tasks_enabled > 0

# A setting changed in MISP's UI or API that git does not hold
- alert: MISPSettingChangedOutsideGit
  expr: misp_settings_manual > 0

# A configure run failed in the last day
- alert: MISPConfigureFailed
  expr: increase(misp_container_job_runs_total{operation="configure",status="error"}[1d]) > 0
```

## Consuming in Kubernetes

With the Prometheus Operator, add a `ServiceMonitor`:

```yaml
# ServiceMonitor
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: misp-metrics
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: metrics
  endpoints:
    - port: metrics
      interval: 30s
```

The metrics pod also carries the annotations that annotation-based discovery reads:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9191"
  prometheus.io/path: "/metrics"
```
