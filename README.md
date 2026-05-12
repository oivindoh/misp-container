# Non-Root MISP Docker Image

A modern, non-root Docker image for [MISP](https://www.misp-project.org/) 2.5, designed for Kubernetes but also usable with Docker Compose.

Key differences from the [official misp-docker](https://github.com/MISP/misp-docker):

- Runs as UID 1000 (no root, no chown/chmod at runtime)
- Separate images for PHP-FPM, Caddy, org sync, and Prometheus metrics
- Init container pattern for volume population
- Fast warm restarts (~0.9s) with DB-backed settings and diff-based configuration
- Much reduced image sizes (~50% reduction, accounting for extra containers like metrics, sync)
- Declarative org/team/user/server/taxonomy/tag/role configuration via YAML file and/or external data source
- S3 attachment storage support promoted to first class citizen (aws-sdk-php included, tested with Garage)
- Python entrypoint scripts (no shell script dependencies, unit-testable)
- Single YAML file for all MISP settings configuration
- Prometheus metrics exporter with server reachability and TLS cert monitoring

## Images

Built from a single Dockerfile with multiple targets:

| Target | Image | Size | Purpose |
|--------|-------|------|---------|
| `final` | `misp` | ~550 MB | PHP-FPM, background workers, init container |
| `caddy` | `misp-caddy` | ~64 MB | Static files + FastCGI reverse proxy (scratch image) |
| `sync` | `misp-sync` | ~175 MB | Declarative org/team/server sync tool |
| `metrics` | `misp-metrics` | ~130 MB | Prometheus metrics exporter |
| `modules` | `misp-modules` | ~296 MB | Enrichment, import, export, action modules (distroless) |

```bash
docker compose build   # builds all images
```

## Quick Start (Docker Compose)

```bash
cd deploy
docker compose build
docker compose up -d
open http://localhost:8080
```

Login: `misp-admin@example.com` / `ChangeMe-Str0ng!Pass#2026` (configured in `deploy/compose.env` and `deploy/compose-secrets.env`).

## Architecture

```
  misp-web Pod                      misp-worker Deployment (scalable)
+----------------------------+     +----------------------------+
| init -> caddy + php-fpm    |     | init -> supervisord        |
|        :8080    :9002      |     |   default, prio, email,    |
+----------------------------+     |   cache, update workers    |
         |            |            +----------------------------+
         |            |
         |    +-------+--------+    misp-scheduler (1 replica)
         |    |                |   +----------------------------+
    +----+----+          +----+---+| init -> supervisord        |
    | MariaDB |          |  Redis ||   scheduler_worker only    |
    +---------+          +--------++----------------------------+

  misp-sync (runs once at startup, then exits)
+----------------------------+
| Declarative org/team/      |
| server config via YAML     |
+----------------------------+

  misp-metrics (long-running)       misp-modules (long-running)
+----------------------------+     +----------------------------+
| Prometheus exporter :9191  |     | Enrichment/import/export   |
| /metrics, /healthz, /ready |     | REST API :6666 (distroless)|
+----------------------------+     +----------------------------+
```

The `misp` image serves four roles (same image, different entrypoint):

| Role | Entrypoint | Description |
|------|------------|-------------|
| **init** | `entrypoint-init.py` | One-shot: extracts distribution files into volumes, generates config files. Runs before web/worker. |
| **web** | `entrypoint-web.py` | PHP-FPM on port 9002. Runs all configuration (with MySQL advisory lock to prevent races between replicas), sets `MISP.live=true` when done. |
| **worker** | `entrypoint-worker.py` | Background job queue workers via supervisord. Does NOT run configuration -- waits for web to set `MISP.live=true`, then starts processing jobs. Safe to scale to multiple replicas. |
| **scheduler** | `entrypoint-worker.py` | Runs only the MISP `scheduler_worker` (no job queues). Processes deferred workflow tasks. Must be a single replica. |

The `misp-caddy` image runs Caddy with a Caddyfile (no entrypoint script) on port 8080. See the [Caddy](#caddy) environment variables for configuration.

The `misp-sync` image applies declarative org/team/server configuration from a YAML file via the MISP REST API. See [Declarative Org Sync](#declarative-org-sync).

The `misp-metrics` image exposes Prometheus metrics on port 9191. See [Metrics Exporter](#metrics-exporter).

The `misp-modules` image runs the [MISP modules](https://github.com/MISP/misp-modules) server on port 6666. See [MISP Modules](#misp-modules).

### Scaling and concurrency

**Workers** can be freely scaled. MISP's SimpleBackgroundJobs uses Redis as the queue backend. `BRPOP` is atomic, so each job is delivered to exactly one worker regardless of how many replicas are running.

**Web replicas** are safe to scale. Configuration uses a MySQL advisory lock (`GET_LOCK('misp_configure', 300)`) so only one replica runs schema upgrades and settings at a time. The lock is connection-scoped -- released automatically on container crash.

**The scheduler** must remain at 1 replica. It polls the MISP `tasks` table for deferred workflow executions. Standard periodic tasks (feed fetching, server sync, taxonomy updates) are handled by Kubernetes CronJobs, so the scheduler is only needed for MISP workflow ad-hoc execution. In Docker Compose (no CronJobs), the scheduler runs inside the worker container by default (`ENABLE_SCHEDULER=true`).

---

## Configuration System

### Settings YAML

All MISP settings are defined in `files/misp-config/settings.yaml`, grouped by configuration phase:

```yaml
settings:
  initialisation:
    MISP.baseurl:
      value: https://localhost
    MISP.language:
      value: eng
    Security.csp_enforce:
      value: true
      since: v2.5.40
```

**Env var override convention:** Any setting can be overridden by an environment variable derived from the setting name -- replace `.` with `_` and uppercase: `MISP.baseurl` -> `MISP_BASEURL`. If the env var exists and is non-empty, the setting is enforced every startup. Otherwise, the default is applied once (then the user owns it via the MISP UI).

**`since`** -- Version-gated default. Re-applied when upgrading to a newer image, even if the setting already exists. Useful for shipping security fixes.

Optional fields:
- `force: true` -- pass `-f` to `cake Admin setSetting`
- `blank_protection: true` -- skip if the env var value is empty

### Groups

Settings are applied in order by group:

1. `minimum_config` -- Bootstrap settings written to `config.php` (Redis host, Python path)
2. `db_enable` -- Toggles `MISP.system_setting_db`
3. `initialisation` -- Core instance settings (baseurl, email, UUID)
4. `critical` -- Always-enforced settings
5. `optional` -- Nice-to-have defaults
6. `gpg` -- GnuPG settings
7. `s3` -- S3 attachment storage (only if `S3_BUCKET` is set)
8. `proxy` -- HTTP proxy (only if `PROXY_ENABLE=true`)

### Setting Storage

With `ENABLE_DB_SETTINGS=true` (recommended), MISP stores settings in two places:

| Location | What |
|----------|------|
| `config.php` | Bootstrap settings (minimum_config group) |
| `system_settings` table | Everything else, persists across restarts |

### Startup Behaviour

Every startup follows the same unified path:

1. Acquires MySQL advisory lock (prevents races between replicas)
2. Sets `MISP.osuser` and runs DB schema migrations
3. Loads all current settings from DB + config.php in one pass
4. For each envar setting: compares against DB, only calls `cake` if different
5. For each default setting: checks if it exists, only calls `cake` if missing
6. Sets up admin user, GPG, auth, S3, proxy
7. Saves defaults version marker
8. Sets `MISP.live=true`

| Scenario | Time |
|----------|------|
| Cold start (empty DB, GPG keygen) | ~10s |
| Warm restart (nothing changed) | ~1s |
| Image upgrade (new defaults) | ~1-2s |
| Env var changed | ~1s |

### Configuration Precedence

1. **Environment variables** (envar type) -- re-applied on every start
2. **MISP web UI** (stored in `system_settings`) -- preserved, but overridden by envars
3. **Defaults** (default type) -- applied once, then user owns it
4. **MISP built-in defaults** -- hardcoded in the application

---

## Declarative Org Sync

The `misp-sync` container applies declarative organisation, user, server, tag, taxonomy, warninglist, and sharing group configuration from a YAML file. It runs once after MISP is ready, then exits. Use it as a startup sidecar or a CronJob for periodic reconciliation.

### Example configuration

See `deploy/orgs.yaml.example` for a full example. Mount your config at `/etc/misp-docker/orgs.yaml`:

```yaml
teams:
  - name: "CERT-Example"
    uuid: "2399b00e-b7f4-4fdb-aeb9-03d28e83a210"
    description: "National CERT"
    sector: "Government"
    nationality: "NO"
    local: true
    default_role: User
    users:
      - email: analyst@example.com
        role: User
      - email: sync@partner.com
        role: Sync user
        authkey: "${PARTNER_SYNC_KEY}"
    servers:
      - name: "Partner MISP"
        url: "https://misp.partner.com"
        authkey: "${PARTNER_AUTHKEY}"
        pull: true
        push: false
        pull_rules:
          tags: ["tlp:clear", "tlp:green"]

tags:
  - name: "release-to:partners"
    colour: "#0088cc"
    exportable: true

taxonomies:
  - tlp
  - admiralty-scale

warninglists:
  - "List of known IPv6 public DNS resolvers"

sharing_groups:
  - name: "Trusted Partners"
    description: "Vetted sharing group"
    releasability: "Members only"
    organisations:
      - uuid: "2399b00e-b7f4-4fdb-aeb9-03d28e83a210"
        extend: true
```

### Features

- **Idempotent**: safe to run repeatedly. Only creates/updates what's needed.
- **Environment variable expansion**: use `${VAR}` in authkeys, URLs, etc.
- **YAML merge support**: split config across files with `!include`.
- **Role management**: `default_role` per team, explicit `role` per user.
- **Server sync rules**: pull/push tag filters, self-signed cert support.
- **Auth key management**: direct bcrypt DB writes (no key invalidation).
- **Disable unmanaged**: optionally disable users/servers not in config.
- **Advisory lock**: acquires the same MySQL advisory lock as the web entrypoint, preventing races when a CronJob fires during a pod restart.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ORG_CONFIG_FILE` | `/etc/misp-docker/orgs.yaml` | Path to org config YAML |
| `ORG_CONFIG_URL` | | URL to fetch config from (alternative to file) |
| `ORG_CONFIG_TOKEN` | | Bearer token for URL fetch |
| `ADMIN_KEY` | | Admin API key (required for sync to work) |
| `SYNC_BASE_URL` | `MISP_BASEURL` | MISP URL the sync container connects to |

---

## Metrics Exporter

The `misp-metrics` container exposes operational metrics in Prometheus exposition format on port 9191.

### Endpoints

| Path | Description |
|------|-------------|
| `/metrics` | Prometheus metrics |
| `/healthz` | Liveness probe (returns 200) |
| `/ready` | Readiness probe (returns 200) |

### Metrics exposed

**Instance health:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_up` | gauge | Whether the MISP database is reachable (1/0) |
| `misp_instance_info` | gauge | Instance metadata (uuid, base_url labels) |

**Content counts:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_events` | gauge | Approximate total events (via information_schema) |
| `misp_attributes` | gauge | Approximate total attributes (via information_schema) |
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
| `misp_server_reachable{id,name,url}` | gauge | Auth-verified connectivity (5min cache) |
| `misp_server_tls_expiry_timestamp_seconds{id,name,url}` | gauge | TLS cert expiry as unix timestamp |

**Background jobs:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_jobs_queued{worker}` | gauge | Current queue depth per worker |
| `misp_server_jobs_total{id,name,job_type,status}` | counter | Pull/push jobs per server |
| `misp_jobs_total{worker,job_type,status}` | counter | Non-sync jobs by worker and type |

**Org sync container:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_sync_runs_24h{operation,status}` | gauge | Org sync runs (from sync log table) |
| `misp_sync_last_success_timestamp_seconds` | gauge | Last successful org sync |

**Self-monitoring:**
| Metric | Type | Description |
|--------|------|-------------|
| `misp_scrape_duration_seconds` | gauge | Collection time |
| `misp_scrape_errors` | gauge | Errors during collection |

### Design decisions

- **No DB cache**: metrics are fresh on every Prometheus scrape. All queries are cheap (information_schema for large tables, exact counts for small tables).
- **Network check cache**: remote server auth probes and TLS cert checks are cached for 5 minutes to avoid hammering sync partners.
- **Counters for jobs**: `misp_server_jobs_total` and `misp_jobs_total` are counters. Use `increase(...[1h])` in PromQL for time-windowed views. MISP prunes completed jobs, which Prometheus handles as counter resets.
- **information_schema for big tables**: `events`, `attributes`, and `shadow_attributes` use InnoDB's `TABLE_ROWS` estimate (~10-20% accuracy) instead of `COUNT(*)` to avoid full index scans on large instances.

### Example alerts

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
```

---

## MISP Modules

The `misp-modules` image provides [MISP modules](https://github.com/MISP/misp-modules) -- optional Python extensions for enrichment, import, export, and workflow actions. It runs on a distroless Debian 13 base (Python 3.13, no shell, no package manager).

### What modules provide

| Type | Examples |
|------|----------|
| **Enrichment** | VirusTotal, Shodan, PassiveTotal, AlienVault OTX, ANY.RUN |
| **Import** | CSV, sandbox reports (Joe, Cuckoo, VMRay), TAXII, email |
| **Export** | YARA rules, CEF, KQL queries, PDF reports |
| **Actions** | Slack, Mattermost, Microsoft Sentinel notifications |

Core MISP (events, sync, API, user management) works without modules. Modules add automated enrichment and format conversion.

### Build variants

Edit `files/requirements-modules.txt` to control which extras are included:

| Requirements line | Size | Modules | Description |
|-------------------|------|---------|-------------|
| `misp-modules==3.0.7` | ~106 MB | ~89 | Core only (import/export, basic enrichment) |
| `misp-modules[minimal]==3.0.7` (default) | ~296 MB | ~131 | Common API clients (VirusTotal, Shodan, YARA, etc.) |
| `misp-modules[all]==3.0.7` | ~500 MB | ~150+ | Everything including numpy, pandas, opencv |

### Configuration

The `MISP_MODULES_FQDN` env var points MISP to the modules service. The default (`http://misp-modules:6666`) matches the Docker Compose service name. All four plugin service URLs (enrichment, import, export, action) use this value.

Individual modules are configured via the MISP UI under **Administration > Server Settings > Plugin**.

---

## Attachment Storage

Attachments are stored separately from MISP distribution files to prevent image upgrades from overwriting user data.

| Path | Content | Managed by |
|------|---------|------------|
| `app/files/` | Taxonomies, galaxies, warninglists, scripts | Init container (replaced on upgrade) |
| `app/attachments/` | User-uploaded file attachments | MISP application (never touched by init) |

**Docker Compose**: both paths are named volumes (`misp-files`, `misp-attachments`). The `ATTACHMENTS_DIR` env var defaults to `/var/www/MISP/app/attachments`.

**Kubernetes**: S3 is the only supported attachment backend. Set the S3 env vars and `MISP.attachments_dir` is automatically set to `s3://`. No PVC needed for attachments.

---

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `MYSQL_HOST` | MariaDB/MySQL hostname |
| `MYSQL_PORT` | MariaDB/MySQL port (default: `3306`) |
| `MYSQL_USER` | Database username |
| `MYSQL_PASSWORD` | Database password |
| `MYSQL_DATABASE` | Database name (default: `misp`) |
| `MISP_REDIS_HOST` | Redis hostname (default: `misp-redis`) |
| `MISP_REDIS_PORT` | Redis port (default: `6379`) |
| `MISP_REDIS_PASSWORD` | Redis password |
| `MISP_BASEURL` | Public URL of the MISP instance |

### Admin User

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_EMAIL` | `admin@admin.test` | Admin email/username |
| `ADMIN_PASSWORD` | | Admin password, set without forced reset |
| `ADMIN_PASSWORD_FILE` | | Path to file containing password (Docker secrets) |
| `ADMIN_ORG` | `ORGNAME` | Admin organisation name |
| `ADMIN_ORG_UUID` | | UUID for admin org (creates if not found, sets `MISP.host_org_id`) |
| `ADMIN_KEY` | | Admin API authkey (40 chars). Used by cronjobs and sync. |
| `ADMIN_KEY_FILE` | | Path to file containing API key |

### S3 Attachment Storage

| Variable | Description |
|----------|-------------|
| `PLUGIN_S3_BUCKET_NAME` | S3 bucket name. Setting this enables S3 mode. |
| `PLUGIN_S3_AWS_ENDPOINT` | S3-compatible endpoint URL |
| `PLUGIN_S3_AWS_ACCESS_KEY` | AWS access key (omit for IAM roles / IRSA) |
| `PLUGIN_S3_AWS_SECRET_KEY` | AWS secret key (omit for IAM roles / IRSA) |

**Important**: MISP's S3 client defaults to `eu-west-1` as the AWS region. For S3-compatible services (MinIO, Garage, Ceph), you must set `Plugin.S3_region` to match the endpoint's region via `cake Admin setSetting` or the MISP UI. For Garage, this is typically `garage`.

### Custom Auth (reverse proxy / oauth2-proxy)

| Variable | Default | Description |
|----------|---------|-------------|
| `CUSTOM_AUTH_ENABLE` | `false` | Enable custom header authentication |
| `CUSTOM_AUTH_HEADER` | `X_FORWARDED_EMAIL` | Header containing the user's email |
| `CUSTOM_AUTH_REQUIRED` | `false` | `true` = header only, `false` = mixed mode |
| `CUSTOM_AUTH_NAME` | `External Authentication` | Display name in the UI |
| `CUSTOM_AUTH_DISABLE_LOGOUT` | `false` | Hide logout button |
| `CUSTOM_AUTH_CUSTOM_LOGOUT` | | URL for external logout |

MISP's CustomAuth matches users by `external_auth_key`, not email. Users need `external_auth_required=true` and `external_auth_key` set to the header value expected from proxy.

See `files/misp_container/admin.py` for the full set of OIDC, LDAP, and CustomAuth variables.

### Multi-Replica and Sync Requirements

| Variable | Where | Required for | Description |
|----------|-------|-------------|-------------|
| `SECURITY_SALT` | Secret | Always | Application salt used for password hashing and CSRF tokens. Must be at least 32 characters, identical across all replicas, and stable across restarts. Without it, MISP auto-generates one -- passwords become invalid after a restart. |
| `MISP_UUID` | ConfigMap | Sync | Instance UUID used to identify this MISP in server-to-server sync. Must be unique per instance and stable across restarts. Without it, MISP auto-generates one that may change on pod restart, breaking sync partnerships. |
| `SECURITY_ENCRYPTION_KEY` | Secret | Encryption | Key for encrypting sensitive fields in the database. |

```bash
# Generate a salt
python3 -c "import secrets; print(secrets.token_hex(32))"
# Generate a UUID
python3 -c "import uuid; print(uuid.uuid4())"
```

The entrypoint logs a warning if `SECURITY_SALT` or `MISP_UUID` are unset, and **refuses to start** if `SECURITY_SALT` is set but shorter than 32 characters.

### PHP / FPM

| Variable | Default | Description |
|----------|---------|-------------|
| `PHP_MEMORY_LIMIT` | `2048M` | PHP memory limit |
| `PHP_MAX_EXECUTION_TIME` | `300` | Max execution time (seconds) |
| `PHP_UPLOAD_MAX_FILESIZE` | `50M` | Max upload file size |
| `PHP_POST_MAX_SIZE` | `50M` | Max POST body size |
| `PHP_FCGI_CHILDREN` | `5` | FPM max children |
| `PHP_FCGI_START_SERVERS` | `2` | FPM start servers |
| `PHP_FCGI_SPARE_SERVERS` | `1` | FPM min spare servers |

### Caddy

| Variable | Default | Description |
|----------|---------|-------------|
| `PHP_FPM_HOST` | `127.0.0.1` | PHP-FPM address. Default is localhost (K8s sidecar). Set to the web service name for Compose. |
| `TRUSTED_PROXY_CIDR` | *(unset)* | Space-separated list of CIDRs (IPv4/IPv6) trusted for `X-Forwarded-For`. When set, Caddy uses the `trusted_proxies` directive to replace the remote address with the client IP from the header. When unset, the direct connection IP is used -- no header is trusted. |

Example for a dual-stack cluster with HAProxy ingress:

```bash
TRUSTED_PROXY_CIDR="10.244.0.0/16 fd00:10:244::/48"
```

This configures `trusted_proxies` in the Caddyfile, so Caddy only trusts `X-Forwarded-For` when the connection originates from those networks. Without this variable, an attacker can spoof the header and MISP will log the spoofed IP.

### Workers

| Variable | Default | Description |
|----------|---------|-------------|
| `NUM_WORKERS_DEFAULT` | `5` | Default queue workers |
| `NUM_WORKERS_PRIO` | `5` | Priority queue workers |
| `NUM_WORKERS_EMAIL` | `5` | Email queue workers |
| `NUM_WORKERS_UPDATE` | `1` | Update queue workers |
| `NUM_WORKERS_CACHE` | `5` | Cache queue workers |
| `ENABLE_SCHEDULER` | `true` | Run the scheduler (set `false` in K8s worker deployment) |

---

## Volume Mounts

| Path | Content | Notes |
|------|---------|-------|
| `app/files/` | Taxonomies, galaxies, warninglists, objects | Populated by init from tarball, replaced on upgrade |
| `app/attachments/` | User-uploaded file attachments | Persistent, never touched by init |
| `app/Config/` | CakePHP config files | Generated by init from env vars |
| `app/tmp/` | CakePHP cache, sessions, logs | Ephemeral |
| `.gnupg/` | GPG keyring | Generated on first run |
| `app/webroot/img/orgs/` | Organisation logos | Ephemeral |
| `app/webroot/img/custom/` | Custom images | Ephemeral |

---

## Kubernetes Deployment

```
deploy/
  base/                         # Shared resources
    base.env                    # Non-secret defaults
    secrets.env                 # Secret defaults (sops-encryptable)
    deployment-web.yaml         # init + caddy + php-fpm pod
    deployment-worker.yaml      # init + worker pod
    deployment-scheduler.yaml   # init + scheduler pod
    deployment-metrics.yaml     # Prometheus metrics exporter
    mariadb.yaml                # MariaDB + Service
    redis.yaml                  # Redis + Service
    ingress.yaml                # HAProxy Ingress
    cronjobs.yaml               # Feed/sync/update CronJobs
    housekeeping-cronjobs.yaml  # DB cleanup CronJobs
    networkpolicy.yaml          # CiliumNetworkPolicies
  overlays/
    test/                       # Test environment
    dev/                        # Dev environment
    prod/                       # Production (KSOPS example)
```

### Secrets Management

Secrets are defined in `.env` files that both kustomize and docker compose can consume. Overlay-specific `.env` files merge with the base using `behavior: merge`.

For production, use KSOPS to decrypt SOPS-encrypted files:
```bash
sops -e -i deploy/base/secrets.env
```

SOPS encrypts values while keeping keys readable. The `.sops.yaml` at the repo root serves as an example and configures encryption via SSH ed25519 key.

### Network Policies

CiliumNetworkPolicies restrict traffic:

| Policy | Ingress from | Egress to |
|--------|-------------|-----------|
| `misp-web` | haproxy-controller ns, cronjobs | MariaDB, Redis, modules :6666, DNS |
| `misp-worker` | none | MariaDB, Redis, DNS |
| `misp-scheduler` | none | MariaDB, Redis, DNS |
| `misp-modules` | web | external HTTPS :443 (RFC 1918 excluded), DNS |
| `misp-metrics` | monitoring ns :9191 | MariaDB, external HTTPS :443, DNS |
| `misp-database` | web, worker, scheduler, housekeeping | DNS |
| `misp-cache` | web, worker, scheduler | DNS |
| `misp-cronjob` | none | web, DNS |
| `misp-housekeeping` | none | database, DNS |

---

## Customising Settings

### custom.yaml

Mount a `custom.yaml` at `/etc/misp-docker/custom.yaml` to override or add settings without modifying the shipped `settings.yaml`:

```yaml
MISP.language:
  group: initialisation
  type: default
  value: nob

Plugin.CustomPlugin_enable:
  group: optional
  type: envar
  value: "${CUSTOM_PLUGIN_ENABLE}"
```

In Kubernetes, mount via ConfigMap. In Compose, bind mount:
```yaml
volumes:
  - ./custom.yaml:/etc/misp-docker/custom.yaml:ro
```

### Custom Scripts

Two hook points for running custom Python scripts during startup:

| Script | When | Use for |
|--------|------|---------|
| `/custom/setup.py` | After DB is ready, before configuration | Schema patches, data imports |
| `/custom/pre-start.py` | After configuration, before PHP-FPM starts | Final tweaks, integrations |

Scripts have access to the `misp_container` library:

```python
from misp_container import cake, db
cake.set_setting("Plugin.MyPlugin_enable", "true")
```

Mount via volume in Compose or ConfigMap in Kubernetes. Optional -- silently skipped if absent.

### Overriding from the MISP UI

Settings changed via **Administration > Server Settings** persist across restarts. However, `type: envar` settings will be overwritten on next startup if the env var differs.

---

## Migration

See [docs/migration.md](docs/migration.md) for migrating from an existing MISP installation.

---

## Development

See [DEVELOPING.md](DEVELOPING.md) for the settings engine internals, release process, test suites, and project structure.
