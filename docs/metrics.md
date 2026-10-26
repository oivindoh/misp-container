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
| `misp_server_reachable{id,name,url}` | gauge | Connectivity, auth-verified when the authkey is stored in clear (checked every 5 minutes). With `Security.encryption_key` set, MISP stores authkeys encrypted and any HTTP answer counts as reachable |
| `misp_server_tls_expiry_timestamp_seconds{id,name,url}` | gauge | TLS cert expiry as unix timestamp |

**Background jobs:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_jobs_queued{worker}` | gauge | Jobs waiting or running per worker queue, read from Redis. Absent when Redis cannot be read |
| `misp_server_jobs_total{id,name,job_type,status}` | counter | Pull/push jobs per server |
| `misp_jobs_total{worker,job_type,status}` | counter | Non-sync jobs by worker and type |
| `misp_scheduled_tasks_enabled{type}` | gauge | Tasks enabled under MISP's Scheduled tasks page. No scheduler runs in this deployment, so these never run; the task runner does the periodic work |

The `status` label of the job counters is `completed`, `failed` or `unfinished`. MISP keeps a
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
- **Queue depth from Redis**: waiting jobs are the queue lists (`LLEN <namespace>:<queue>`) and running jobs the `<namespace>:running:<queue>:<id>` keys, in the database and namespace of `SimpleBackgroundJobs.redis_*`. A failed Redis read counts in `misp_scrape_errors`.
- **Counters for jobs**: `misp_server_jobs_total` and `misp_jobs_total` are counters. Use `increase(...[1h])` in PromQL for time-windowed views. MISP prunes completed jobs, which Prometheus handles as counter resets.
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

# Pull failures increasing
- alert: MISPPullFailures
  expr: increase(misp_server_jobs_total{job_type="pull",status="failed"}[1h]) > 0

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

Add a `ServiceMonitor` (if using prometheus-operator) or scrape annotations:

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

Or use pod annotations for annotation-based discovery:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9191"
  prometheus.io/path: "/metrics"
```
