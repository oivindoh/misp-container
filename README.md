# MISP Container

A modern Docker image for [MISP](https://www.misp-project.org/) 2.5, designed for Kubernetes but also usable with Docker Compose.

## Goals

1. **No root, no privileges, no writable filesystem** -- runs as UID 1000, read-only root filesystem, all capabilities dropped
2. **Smaller image** -- ~550 MB for the main image (~50% smaller than upstream, accounting for extra containers)
3. **Scalable** -- multiple web and worker replicas in Kubernetes, MySQL advisory lock prevents configuration races
4. **Easy to consume as a Kustomize base** -- `deploy/base/` is a complete, opinionated Kustomize base with overlay examples for environment-specific config
5. **Enterprise-ready** -- declarative user/org/server management, Prometheus metrics, S3 storage, OIDC/LDAP/header auth, CiliumNetworkPolicies

## Images

Built from a single Dockerfile with multiple targets:

| Target | Image | Size | Purpose |
|--------|-------|------|---------|
| `final` | `misp` | ~550 MB | PHP-FPM, background workers, init container |
| `caddy` | `misp-caddy` | ~64 MB | Static files + reverse proxy (scratch image) |
| `sync` | `misp-sync` | ~175 MB | Declarative org/team/server sync tool |
| `metrics` | `misp-metrics` | ~130 MB | Prometheus metrics exporter |
| `modules` | `misp-modules` | ~296 MB | MISP enrichment/import/export modules (distroless) |

## Quick start

```bash
cd deploy
docker compose build
docker compose up -d
open http://localhost:8080
```

Default login: `admin@admin.test` / `ChangeMe-Str0ng!Pass#2026`

## Architecture

```
  web Pod                           worker Deployment (scalable)
+----------------------------+     +----------------------------+
| init -> caddy + php-fpm    |     | init -> supervisord        |
|        :8080    :9002      |     |   default, prio, email,    |
+----------------------------+     |   cache, update workers    |
         |            |            +----------------------------+
         |            |
         |    +-------+--------+    scheduler (1 replica)
         |    |                |   +----------------------------+
    +----+----+          +----+---+| init -> supervisord        |
    | MariaDB |          |  Redis ||   scheduler_worker only    |
    +---------+          +--------++----------------------------+
```

The `misp` image serves four roles (same image, different entrypoint):

| Role | Entrypoint | Description |
|------|------------|-------------|
| **init** | `entrypoint-init.py` | One-shot: extracts files into volumes, generates config. Runs before web/worker. |
| **web** | `entrypoint-web.py` | PHP-FPM on port 9002. Runs configuration with advisory lock, sets `MISP.live=true`. |
| **worker** | `entrypoint-worker.py` | Background job workers via supervisord. Waits for `MISP.live=true`, then processes jobs. Safe to scale. |
| **scheduler** | `entrypoint-worker.py` | Runs only the MISP `scheduler_worker`. Must be a single replica. |

Additional containers: **caddy** (reverse proxy), **sync** (declarative org config), **metrics** (Prometheus exporter), **modules** (enrichment/import/export).

### Scaling

- **Workers**: freely scalable. Redis `BRPOP` delivers each job to exactly one worker.
- **Web**: safely scalable. Configuration uses a MySQL advisory lock.
- **Scheduler**: must remain at 1 replica.

---

## Configuration

### Settings YAML

All MISP settings are defined in `files/misp-config/settings.yaml`. Every setting can be overridden by an environment variable derived from its name:

```
MISP.redis_host  ->  MISP_REDIS_HOST
Plugin.S3_bucket_name  ->  PLUGIN_S3_BUCKET_NAME
```

If the env var exists and is non-empty, the setting is enforced on every startup. If no env var is set, the default from `settings.yaml` is applied once, then the user owns it via the MISP UI.

See `deploy/base/base.env` for the container-level defaults and `deploy/base/secrets.env` for secrets.

### Startup behaviour

1. Acquires MySQL advisory lock (prevents races between replicas)
2. Runs DB schema migrations and performance indexes
3. Loads all current settings from DB in one pass
4. Compares desired state against actual, only calls `cake` when different
5. Sets up admin user, GPG, auth
6. Sets `MISP.live=true`

Warm restarts (nothing changed) take ~1 second.

### Essential variables

| Variable | Description |
|----------|-------------|
| `MISP_BASEURL` | Public URL of the instance |
| `ADMIN_EMAIL` | Admin email/username |
| `ADMIN_PASSWORD` | Admin password |
| `SECURITY_SALT` | Password hashing salt. Must be 32+ chars, identical across replicas, stable across restarts. |
| `MISP_UUID` | Instance UUID for server sync. Must be unique and stable. |

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"  # generate salt
python3 -c "import uuid; print(uuid.uuid4())"              # generate UUID
```

Database and Redis connection details are in `deploy/base/base.env`.

### HTTPS

Caddy supports automatic HTTPS via Let's Encrypt. Set `CADDY_ADDRESS` to a domain name to enable it:

```yaml
environment:
  CADDY_ADDRESS: misp.example.com
```

When unset, Caddy serves plain HTTP on `:8080` (suitable when behind a load balancer).

---

## Declarative Org Sync

The `misp-sync` container applies declarative organisation, user, server, tag, taxonomy, warninglist, and sharing group configuration from a YAML file. It runs once after MISP is ready, then exits.

See `deploy/orgs.yaml.example` for a full example. Mount your config at `/etc/misp-docker/orgs.yaml`:

```yaml
teams:
  - name: "CERT-Example"
    uuid: "2399b00e-b7f4-4fdb-aeb9-03d28e83a210"
    sector: "Government"
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

taxonomies:
  - tlp
  - admiralty-scale
```

Features: idempotent, environment variable expansion in authkeys/URLs, role management, server sync rules with tag filters, advisory lock for safe concurrent operation.

| Variable | Description |
|----------|-------------|
| `ORG_CONFIG_FILE` | Path to config YAML (default: `/etc/misp-docker/orgs.yaml`) |
| `ORG_CONFIG_URL` | URL to fetch config from (alternative to file) |
| `ADMIN_KEY` | Admin API key (required) |
| `SYNC_BASE_URL` | MISP URL the sync container connects to |

### Custom scripts

Two hook points for custom Python during startup:

| Script | When |
|--------|------|
| `/custom/setup.py` | After DB ready, before configuration |
| `/custom/pre-start.py` | After configuration, before PHP-FPM starts |

Mount via volume. Optional -- silently skipped if absent.

---

## Kubernetes

```
deploy/
  base/                 # Kustomize base (all resources)
  overlays/
    prod/               # Production overlay (KSOPS secrets example)
```

Use as a Kustomize base and override per environment:

```yaml
# your-overlay/kustomization.yaml
resources:
  - ../../base
configMapGenerator:
  - name: misp-env
    behavior: merge
    literals:
      - MISP_BASEURL=https://misp.example.com
      - ADMIN_EMAIL=admin@example.com
```

Secrets are `.env` files consumable by both Kustomize and Docker Compose. Encrypt with SOPS for production:
```bash
sops -e -i deploy/base/secrets.env
```

### Network policies

CiliumNetworkPolicies in `deploy/base/networkpolicy.yaml` restrict all traffic to the minimum required paths. See the file header for the full traffic flow diagram.

---

## Metrics

A Prometheus metrics exporter is included (`misp-metrics` image, port 9191). Covers instance health, content counts, server sync status, background job queues, TLS cert expiry, and org sync runs.

See [docs/metrics.md](docs/metrics.md) for the full metrics reference and example alerts.

---

## Attachment Storage

| Backend | When |
|---------|------|
| **Local volume** | Docker Compose (default). Named volume `misp-attachments`. |
| **S3** | Kubernetes (recommended). Set `PLUGIN_S3_BUCKET_NAME` and endpoint/credentials. |

---

## Migration

See [docs/migration.md](docs/migration.md) for migrating from an existing MISP installation.

## Development

See [DEVELOPING.md](DEVELOPING.md) for the settings engine internals, release process, and test suites.
