# MISP Container

A container image for [MISP](https://www.misp-project.org/) 2.5, designed for Kubernetes but also usable with Compose (podman).

## Goals

1. **No root, no privileges, no writable filesystem** -- runs as UID 1000, read-only root filesystem, all capabilities dropped
2. **Smaller image** -- ~680 MB for the main image, with the MISP distribution files baked in and nothing extracted at start
3. **Scalable** -- multiple web and worker replicas in Kubernetes, MySQL advisory lock prevents configuration races
4. **Easy to consume as a Kustomize base** -- `deploy/base/` is a complete, opinionated Kustomize base with overlay examples for environment-specific config
5. **Enterprise-ready** -- declarative user/org/server management, Prometheus metrics, S3 storage, OIDC/LDAP/header auth, CiliumNetworkPolicies

## Images

Built from a single Dockerfile with three targets:

| Target | Image | Size | Purpose |
|--------|-------|------|---------|
| `final` | `misp` | ~680 MB | PHP-FPM, background workers, configure, org sync, metrics exporter (one entrypoint per role); `app/files` ships in the image |
| `caddy` | `misp-caddy` | ~64 MB | Static files + reverse proxy (scratch image) |
| `modules` | `misp-modules` | ~296 MB | MISP enrichment/import/export modules (distroless) |

## Quick start

```bash
cd deploy
podman compose build
podman compose up -d
open http://localhost:8080
```

Default login: `admin@admin.test` / `ChangeMe-Str0ng!Pass#2026`

## Architecture

```
  configure Job (runs first, once per rollout)
+---------------------------------------------------------------+
| schema, settings, admin, GPG, auth -> MISP.live=true           |
+---------------------------------------------------------------+

  web Pod (scalable)                worker Deployment (scalable)
+----------------------------+     +----------------------------+
| caddy + php-fpm            |     | supervisord                |
| :8080    :9002             |     |   default, prio, email,    |
+----------------------------+     |   cache, update workers    |
         |            |            +----------------------------+
         |            |
         |    +-------+--------+    scheduler (1 replica)
         |    |                |   +----------------------------+
    +----+----+          +----+---+| supervisord                |
    | MariaDB |          |  Redis ||   scheduler_worker only    |
    +---------+          +--------++----------------------------+
```

The `misp` image serves six roles (same image, different entrypoint). Every entrypoint renders `app/Config` and imports the GPG key at start:

| Role | Entrypoint | Description |
|------|------------|-------------|
| **configure** | `entrypoint-configure.py` | One-shot Job per rollout: schema migrations, settings, admin user, GPG, auth. Sets `MISP.live=true` last. |
| **web** | `entrypoint-web.py` | PHP-FPM on port 9002. Waits for `MISP.live=true`, then serves. Safe to scale. |
| **worker** | `entrypoint-worker.py` | Background job workers via supervisord. Waits for `MISP.live=true`, then processes jobs. Safe to scale. |
| **scheduler** | `entrypoint-worker.py` | Runs only the MISP `scheduler_worker`. Must be a single replica. |
| **org-sync** | `entrypoint-sync.py` | One-shot Job after each rollout: applies `orgs.yaml` through the API. |
| **metrics** | `entrypoint-metrics.py` | Prometheus exporter on port 9191. |

Additional images: **caddy** (reverse proxy) and **modules** (enrichment/import/export).

### Scaling

- **Workers**: freely scalable. Redis `BRPOP` delivers each job to exactly one worker. A stopping worker gets `WORKER_STOP_GRACE` seconds (default 300) to finish its job; a job on a worker that dies is lost.
- **Web**: freely scalable. Web pods never run configuration; the configure Job does, under a MySQL advisory lock.
- **Scheduler**: must remain at 1 replica. It serves MISP-internal scheduling (workflows). Periodic tasks belong to the `cronjobs` component; do not enable the same tasks under MISP's Scheduled tasks, or both run them.

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

The configure step runs once per rollout: a Job in Kubernetes, a one-shot service in Compose.

1. Acquires the MySQL advisory lock (a concurrent org sync waits)
2. Runs DB schema migrations and performance indexes
3. Loads all current settings from DB in one pass
4. Compares desired state against actual, only calls `cake` when different
5. Sets up admin user, GPG, auth
6. Sets `MISP.live=true`

Web and worker pods wait for `MISP.live=true`, then start PHP-FPM or supervisord. They never
touch the schema or the settings, so they start in seconds and scale freely.

### Essential variables

| Variable | Description |
|----------|-------------|
| `MISP_BASEURL` | Public URL of the instance |
| `ADMIN_EMAIL` | Admin email/username |
| `ADMIN_PASSWORD` | Admin password |
| `SECURITY_SALT` | Password hashing salt. Must be 32+ chars, identical across replicas, stable across restarts. |
| `MISP_UUID` | Instance UUID for server sync. Must be unique and stable. |

The configure step refuses to run with a placeholder secret (`change-me`, `override-me`, an all-zero salt), a salt shorter than 32 characters, or an empty `MISP_UUID`.

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"  # generate salt
python3 -c "import uuid; print(uuid.uuid4())"              # generate UUID
```

Database and Redis connection details are in `deploy/base/base.env`. `MISP_REDIS_*` also fills the
background-job and ZeroMQ Redis settings, and `MISP_BASEURL` fills the external and REST client base URLs,
unless those are set explicitly.

### HTTPS

Caddy supports automatic HTTPS via Let's Encrypt. Set `CADDY_ADDRESS` to a domain name to enable it:

```yaml
environment:
  CADDY_ADDRESS: misp.example.com
```

When unset, Caddy serves plain HTTP on `:8080` (suitable when behind a load balancer).

---

## Declarative Org Sync

The org-sync entrypoint applies declarative organisation, user, server, tag, taxonomy, warninglist, and sharing group configuration from a YAML file. It runs once after MISP is ready, then exits: the `org-sync` Job in Kubernetes (sync wave 3), the `sync` service in Compose.

See `deploy/orgs.yaml.example` for a full example. In Kubernetes, replace the `misp-orgs` ConfigMap in your overlay:

```yaml
configMapGenerator:
  - name: misp-orgs
    behavior: replace
    files:
      - orgs.yaml=orgs.yaml
```

In Compose, mount the file at `/etc/misp-docker/orgs.yaml` on the `sync` service:

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

### GPG key

MISP signs notification email with an instance GPG key. Every replica must hold the same
key, so the key is supplied, not generated:

```bash
gpg --batch --passphrase "$GNUPG_PASSWORD" --quick-generate-key "MISP Admin <misp@example.com>" rsa3072 sign never
gpg --armor --export-secret-keys misp@example.com > private.asc
kubectl -n misp create secret generic misp-gnupg --from-file=private.asc
```

Every entrypoint imports `private.asc` from the optional Secret `misp-gnupg` at start. `GNUPG_PASSWORD` must match the key passphrase. Set `GNUPG_SIGN=true` once the key is
in place. Compose sets `AUTOCONF_GPG=true` instead, which generates a key in the
`misp-gnupg` volume on first start.

### Custom scripts

Two hook points for custom Python during startup:

| Script | Where | When |
|--------|-------|------|
| `/custom/setup.py` | configure | After DB ready, before configuration |
| `/custom/pre-start.py` | web | In every web replica, before PHP-FPM starts |

Mount via volume. Optional -- silently skipped if absent.

---

## Kubernetes

```
deploy/
  base/                 # MISP itself: Jobs, Deployments, Services, config, attachments claim
  components/           # Optional parts an overlay opts into
  overlays/
    prod/               # Production overlay (KSOPS secrets example, all components)
```

| Component | Content | Leave it out when |
|-----------|---------|-------------------|
| `mariadb` | StatefulSet with a 20Gi PVC | You run an external database (`MYSQL_HOST`) |
| `redis` | Deployment without persistence | You run an external Redis (`MISP_REDIS_HOST`) |
| `ingress-haproxy` | Ingress for the haproxy class | Another ingress or a Gateway routes to the `web` Service |
| `netpol-cilium` | CiliumNetworkPolicies for every pod | The cluster does not run Cilium |
| `cronjobs` | Feed, sync and update CronJobs through the API (`python3 -m misp_container.task <task>`) | The MISP scheduler runs these tasks |
| `housekeeping` | Nightly deletes in `jobs`, `logs`, `audit_logs` | Retention is handled elsewhere |
| `pdb` | PodDisruptionBudgets for web and worker | One replica of each |

The `configure` and `org-sync` Jobs carry Argo CD sync annotations: configure runs in sync
wave 1, the Deployments in wave 2, org-sync in wave 3, and Argo CD recreates both Jobs on
every sync. With plain `kubectl apply`, delete a finished Job before applying a changed spec:

```bash
kubectl delete job configure org-sync
```

Use the base plus the components you need, and override per environment:

```yaml
# your-overlay/kustomization.yaml
resources:
  - ../../base
components:
  - ../../components/mariadb
  - ../../components/redis
  - ../../components/ingress-haproxy
configMapGenerator:
  - name: misp-env
    behavior: merge
    literals:
      - MISP_BASEURL=https://misp.example.com
      - ADMIN_EMAIL=admin@example.com
```

Secrets are `.env` files consumable by both Kustomize and Compose. `deploy/base/secrets.env`
holds placeholders that the configure step refuses. For production, supply the real Secret
from your overlay through KSOPS, as `deploy/overlays/prod` does: an encrypted `secrets.sops.yaml`
with `kustomize.config.k8s.io/behavior: replace`. Kustomize's `secretGenerator` cannot decrypt,
so do not encrypt `secrets.env` in place.

### Storage

| Resource | Base default | Notes |
|----------|--------------|-------|
| MariaDB | StatefulSet, 20Gi ReadWriteOnce PVC | Patch the size or storage class in the overlay |
| Attachments | PVC `attachments`, 20Gi ReadWriteMany | Web and worker pods share it. Use S3 instead by setting `PLUGIN_S3_BUCKET_NAME` and removing the claim |
| Redis | No persistence | Sessions and queued jobs are lost when Redis restarts |

The web and worker entrypoints refuse to start when the attachments directory is not
writable and S3 is not configured.

### Network policies

The `netpol-cilium` component restricts all traffic to the minimum required paths. See the header of `deploy/components/netpol-cilium/networkpolicy.yaml` for the full traffic flow diagram.

### Periodic tasks

The task runner in the main image calls the MISP API for the periodic work: `cache-feeds`,
`fetch-feeds`, `pull-servers`, `push-servers`, `update-galaxies`, `update-taxonomies`,
`update-warninglists`, `update-noticelists`. The `cronjobs` component schedules them in
Kubernetes. In Compose, run one on demand:

```bash
podman compose run --rm --no-deps sync python3 -m misp_container.task pull-servers
```

`ADMIN_KEY` must be set; `SYNC_BASE_URL` defaults to `MISP_BASEURL`.

---

## Metrics

A Prometheus metrics exporter is included (`entrypoint-metrics.py` in the main image, port 9191). Covers instance health, content counts, server sync status, background job queues, TLS cert expiry, and org sync runs.

See [docs/metrics.md](docs/metrics.md) for the full metrics reference and example alerts.

---

## Attachment Storage

| Backend | When |
|---------|------|
| **Local volume** | Compose: named volume `misp-attachments`. Kubernetes: PVC `attachments` (ReadWriteMany). |
| **S3** | Set `PLUGIN_S3_BUCKET_NAME` and endpoint/credentials. In Kubernetes, remove the `attachments` claim in the overlay. |

---

## Migration

See [docs/migration.md](docs/migration.md) for migrating from an existing MISP installation.

## Development

See [DEVELOPING.md](DEVELOPING.md) for the settings engine internals, release process, and test suites.
