# MISP Container

A container image for [MISP](https://www.misp-project.org/) 3.0 on PostgreSQL, and a Helm
chart that runs it on Kubernetes.

MISP has not released 3.0 yet: the image runs MISP's `3.0_wip` branch, pinned to a commit,
and its releases are the pre-releases `3.0.0-wip.N`. A MISP 2.x database comes in through the
migrate Job. See [docs/misp-3.md](docs/misp-3.md).

## TL;DR

- One image, one module per role: a `configure` Job sets the database and settings up,
  then web and worker pods start and scale freely. Two small extra images: caddy and modules.
- Non-root (UID 1000), read-only root filesystem, all capabilities dropped, sessions in Redis.
- Every MISP setting has an env var (`MISP.redis_host` -> `MISP_REDIS_HOST`). Curated defaults
  live in `files/misp-config/settings.yaml`; every other setting is catalogued.
- Kubernetes: the Helm chart `deploy/chart` renders MISP itself; components switch on the
  optional parts (database, cache, ingress or HTTPRoute, network policies, CronJobs for all
  periodic work, PDBs). Each release publishes it to GHCR.
- On one machine, k3s in podman runs the chart ([Quick start](#quick-start)). The test suites
  run it the same way.

## Images

One Dockerfile, three targets:

| Target | Image | On disk | Pull (compressed) | Purpose |
|--------|-------|---------|-------------------|---------|
| `final` | `misp-container` | 980 MB | 270 MB | PHP-FPM, workers, configure, org sync, metrics exporter, task runner, migrate, and the Python packages MISP's own scripts import (STIX, TAXII, ZeroMQ, sample extraction); `app/files` (360 MB of lists, galaxies and geolocation data) ships in the image |
| `caddy` | `misp-container-caddy` | 84 MB | 31 MB | Static files and FastCGI reverse proxy (scratch image) |
| `modules` | `misp-container-modules` | 360 MB | 98 MB | MISP enrichment, import, export and action modules (distroless) |

Sizes are arm64 builds of MISP `3.0_wip` at commit 8888b98; the compressed size is the gzip of
the image's layers.

Image tags are the release versions, `3.0.0-wip.N` while MISP 3.0 is a work in progress. See
[DEVELOPING.md](DEVELOPING.md) for releases.

## Quick start

k3s in podman runs the chart on one machine. On macOS, k3s needs the podman machine's rootful
connection: add `--connection podman-machine-default-root` to each `podman` command.

```bash
podman run -d --name k3s --privileged -p 6443:6443 \
    docker.io/rancher/k3s:v1.37.1-k3s1 server --disable traefik
podman exec k3s cat /etc/rancher/k3s/k3s.yaml > k3s.yaml
export KUBECONFIG=$PWD/k3s.yaml
cat > values.yaml <<EOF
env:
  MISP_BASEURL: http://localhost:8080
  MISP_UUID: $(python3 -c "import uuid; print(uuid.uuid4())")
secrets:
  db: {DB_PASSWORD: $(openssl rand -hex 16)}
  app: {MISP_REDIS_PASSWORD: $(openssl rand -hex 16), GNUPG_PASSWORD: $(openssl rand -hex 16), SECURITY_SALT: $(openssl rand -hex 32)}
  admin: {ADMIN_PASSWORD: $(openssl rand -hex 16)}
postgres: {enabled: true}
redis: {enabled: true}
attachments: {accessMode: ReadWriteOnce}
EOF
helm install misp oci://ghcr.io/oivindoh/charts/misp --devel \
    --namespace misp --create-namespace -f values.yaml
kubectl -n misp port-forward svc/web 8080:8080
```

Log in at `http://localhost:8080` as `admin@admin.test`, with the `ADMIN_PASSWORD` of
`values.yaml`. `podman rm -f k3s` deletes the cluster. From a checkout, `mise run build` builds
the images; [DEVELOPING.md](DEVELOPING.md) shows how a test suite runs them on k3s.

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
Each release publishes the chart as `oci://ghcr.io/oivindoh/charts/misp`. The chart has its
own SemVer version, and its `appVersion` is the MISP version of the images. Helm, Argo CD or
Flux installs it ([docs/kubernetes.md](docs/kubernetes.md#install)), which also shows the
values and lists the components, Secrets, storage, network policies and periodic tasks.

## Documentation

| Document | Covers |
|----------|--------|
| [docs/architecture.md](docs/architecture.md) | The roles, the configure Job, startup and footprint, rollout order, scaling |
| [docs/configuration.md](docs/configuration.md) | Settings and their env vars, database, HTTPS, authentication plugins and Keycloak, logging |
| [docs/kubernetes.md](docs/kubernetes.md) | Install, values, components, client addresses, Secrets, storage, network policies, periodic tasks |
| [docs/org-sync.md](docs/org-sync.md) | Organisations, users, servers, tags and taxonomies from a YAML file |
| [docs/metrics.md](docs/metrics.md) | The Prometheus metrics and example alerts |
| [docs/migration.md](docs/migration.md) | Copying an existing MISP database and its attachments into a deployment |
| [docs/operations.md](docs/operations.md) | Backup and restore, keys and secrets, upgrades |
| [docs/misp-3.md](docs/misp-3.md) | The MISP 3.0 pin, its versions, a 2.x database, and how 3.0 differs from 2.5 |
| [DEVELOPING.md](DEVELOPING.md) | The settings engine, the tests, new MISP releases, the release process |
| [tests/README.md](tests/README.md) | The test suites |
| [AGENTS.md](AGENTS.md) | A map of the tree for agents, generated from the tree (`mise run agents-md`) |
