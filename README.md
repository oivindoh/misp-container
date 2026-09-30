# MISP Container

A container image for [MISP](https://www.misp-project.org/) 2.5, built for Kubernetes and
usable with Compose (podman) for development.

## TL;DR

- One image, one entrypoint per role: a `configure` Job sets the database and settings up,
  then web and worker pods start and scale freely. Two small extra images: caddy and modules.
- Non-root (UID 1000), read-only root filesystem, all capabilities dropped, sessions in Redis.
- Every MISP setting has an env var (`MISP.redis_host` -> `MISP_REDIS_HOST`). Curated defaults
  live in `files/misp-config/settings.yaml`; every other setting is catalogued.
- Kubernetes: the Helm chart `deploy/chart` renders MISP itself; components switch on the
  optional parts (database, cache, ingress or HTTPRoute, network policies, CronJobs for all
  periodic work, PDBs). Each release publishes it to GHCR.
- Compose: `cd deploy && podman compose up -d`, login `admin@admin.test` /
  `ChangeMe-Str0ng!Pass#2026` at `http://localhost:8080`.

## Images

One Dockerfile, three targets:

| Target | Image | On disk | Pull (compressed) | Purpose |
|--------|-------|---------|-------------------|---------|
| `final` | `misp-container` | 890 MB | 250 MB | PHP-FPM, workers, configure, org sync, metrics exporter, task runner, migrate; `app/files` (360 MB of lists, galaxies and geolocation data) ships in the image |
| `caddy` | `misp-container-caddy` | 76 MB | 29 MB | Static files and FastCGI reverse proxy (scratch image) |
| `modules` | `misp-container-modules` | 360 MB | 98 MB | MISP enrichment, import, export and action modules (distroless) |

Sizes are the arm64 build of MISP 2.5.47; amd64 is within a few percent.

Image tags match the MISP version (`2.5.47`, hotfixes `2.5.47-r1`). See
[DEVELOPING.md](DEVELOPING.md) for releases.

## Quick start

```bash
cd deploy
podman compose build
podman compose up -d
open http://localhost:8080
```

Default login: `admin@admin.test` / `ChangeMe-Str0ng!Pass#2026`.

`deploy/docker-compose.yml` is the single-host development stack: the same images and
entrypoints, named volumes instead of claims, `AUTOCONF_GPG=true` (a key generated on first
start), and no CronJobs: run periodic tasks on demand
(see [Periodic tasks](docs/kubernetes.md#periodic-tasks)). Values come from
`deploy/chart/files/*.env`, the chart's defaults, with `deploy/compose.env` and
`deploy/compose-secrets.env` on top.
`MISP_IMAGE_TAG` selects the image tag.

## Essential variables

| Variable | Description |
|----------|-------------|
| `MISP_BASEURL` | Public URL of the instance |
| `ADMIN_EMAIL` | Admin email and username |
| `ADMIN_PASSWORD` | Admin password, 12 characters or more |
| `SECURITY_SALT` | Password hashing salt: 32 characters or more, identical on every replica, stable across restarts |
| `MISP_UUID` | Instance UUID for server sync: unique and stable |

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"  # salt
python3 -c "import uuid; print(uuid.uuid4())"              # UUID
```

Every other setting has an env var too; see [docs/configuration.md](docs/configuration.md).

## Kubernetes

The Helm chart in `deploy/chart` renders MISP itself; each component (database, cache,
ingress or HTTPRoute, network policies, CronJobs) switches on with `<component>.enabled`.
Each release publishes the chart as `oci://ghcr.io/oivindoh/charts/misp`; its version is the
release tag without the `v`. Helm, Argo CD or Flux installs it:

```bash
helm install misp oci://ghcr.io/oivindoh/charts/misp --version <release> \
    --namespace misp --create-namespace -f values.yaml
```

[docs/kubernetes.md](docs/kubernetes.md) shows the values and lists the components, Secrets,
storage, network policies and periodic tasks.

## Documentation

| Document | Covers |
|----------|--------|
| [docs/architecture.md](docs/architecture.md) | The roles, the configure Job, startup and footprint, rollout order, scaling |
| [docs/configuration.md](docs/configuration.md) | Settings and their env vars, database, HTTPS, authentication plugins, custom scripts, logging |
| [docs/kubernetes.md](docs/kubernetes.md) | Install, values, components, client addresses, Secrets, storage, network policies, periodic tasks |
| [docs/org-sync.md](docs/org-sync.md) | Organisations, users, servers, tags and taxonomies from a YAML file |
| [docs/metrics.md](docs/metrics.md) | The Prometheus metrics and example alerts |
| [docs/migration.md](docs/migration.md) | Copying an existing MISP database and its attachments into a deployment |
| [DEVELOPING.md](DEVELOPING.md) | The settings engine, the tests, new MISP releases, the release process |
| [tests/README.md](tests/README.md) | The test suites |
| [AGENTS.md](AGENTS.md) | A map of the tree for agents, generated from the tree (`mise run agents-md`) |
