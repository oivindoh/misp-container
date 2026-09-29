# Testing

## TL;DR

Four suites: unit tests on the Python library, an integration suite on one Compose stack, a hub-spoke sync suite on three instances, and a migration suite that copies a seeded instance onto MariaDB and PostgreSQL. Two Kubernetes checks cover the Kustomize base: every render validated against the schemas, and the base applied to a kind cluster with the smoke test.

```bash
mise run test                # unit tests (~0.2s)
mise run test-integration    # single-instance integration tests (~70s)
mise run test-integration -- --postgres   # the same on PostgreSQL
mise run test-sync           # hub-spoke sync test with 3 instances (~90s)
mise run test-migration      # migrate Job: MariaDB to MariaDB, MariaDB to PostgreSQL (~3min)
mise run test-all            # every suite
mise run test-kustomize      # render the base with each component, validate against the schemas (~10s)
mise run test-kind           # the base on a kind cluster, then the smoke test (build the images first)
```

All integration tests build the images with compose, start full MISP stacks, and tear them down automatically. Pass `--skip-build` to reuse existing images.

## Unit tests

**271 tests** covering the Python entrypoint library (`files/misp_container/`).

| File | What it tests |
|------|---------------|
| `test_config.py` | Settings diff engine, version comparison, YAML loading, env var expansion, settings cache |
| `test_env.py` | Environment variable defaults, `apply_defaults()`, worker config derivation, derived variables |
| `test_task.py` | Periodic task runner (cronjob entrypoint) |
| `test_db.py` | Advisory lock holds one connection until release, statement splitter, the version gate |
| `test_engine.py` | Engine selection, SQL fragments per engine, `MYSQL_*` aliases, housekeeping batches, cursor handling |
| `test_configure.py` | Placeholder and identity checks of the configure step |
| `test_config_php.py` | config.php rendering from settings.yaml, PHP escaping and typing |
| `test_init.py` | app/Config rendering, database.php/email.php generation, GPG key import, writable check |
| `test_admin.py` | SQL escape function |
| `test_sync.py` | Org sync engine: config normalization, merge logic, UUID validation, env expansion, role/org/tag/user/server/taxonomy/warninglist/sharing group apply logic, build rules (pull vs push tag format), allow_external user placement, default_role, disable unmanaged resources, full orchestrator flow |

Run with: `mise run test` or `PYTHONPATH=files python -m pytest tests/ -v`

## Integration tests

**111 tests** verifying the full MISP stack in Compose.

**Stack:** 1 MISP instance (configure + web x2 + caddy + worker + MariaDB or PostgreSQL + Redis + Garage S3 + dex)

| Suite | Tests | What it verifies |
|-------|-------|------------------|
| Non-root operation | 3 | All containers run as UID 1000 |
| HTTP / Caddy | 4 | Login page, static CSS, root redirect |
| Admin user config | 5 | Email, password (no forced reset), org name, org UUID, last_pw_change |
| Database settings | 3 | DB persistence, setting count, BASE_URL in DB |
| Workers | 7 | The five queues run under supervisord, no scheduler program, web reaches supervisord over TCP |
| Background jobs | 1 | Event publish triggers job, worker completes it (status=4) |
| PHP-FPM | 1 | Listening on port 9002 |
| Distribution files and app/Config | 11 | taxonomies in the image, bootstrap.php patch, database.php host, config.php content |
| GPG | 1 | Auto-generated key in .gnupg volume |
| MISP API | 2 | Version endpoint, event create via API |
| Warm restart | 2 | Settings cache reload, minimum_config unchanged |
| Version-gated defaults | 3 | Defaults version saved, version gate stability, envar precedence |
| S3 attachment storage | 4 | Garage S3 bootstrap, upload, download, bucket verification |
| Custom auth | 2 | Header login (200), no-header redirect (302) |
| OIDC login | 7 | Redirect to dex, its form, the callback, the user's email, role by name, default organisation, mixed auth |
| Multi-replica web | 6 | configure service ran once, two web replicas serve without configuring |
| Settings and scheduler coverage | 3 | Every MISP setting curated or catalogued, every scheduler task covered by a task, no rejected `cake` setting |

**Files:**
- `run-integration-tests.sh` -- test script
- `docker-compose.test.yml` -- overlay on `deploy/docker-compose.yml` (test ports, env, Garage S3, dex)
- `docker-compose.postgres.yml`, `postgres.env` -- second overlay for `--postgres`: points every MISP container at the postgres service
- `dex.yaml` -- the OIDC provider's static client and user
- `test-compose.env` -- test env overrides (BASE_URL, ADMIN_EMAIL, etc.)
- `test-compose-secrets.env` -- test secrets (passwords, Redis key), layered over `deploy/base/secrets-*.env`
- `garage.toml` -- Garage S3 config for attachment testing

Run with: `mise run test-integration`

## Kubernetes checks

**Render check** (`scripts/check-kustomize.sh`): renders the base alone, with each component,
and with every component, and validates each render with `kubeconform -strict` against the
Kubernetes schemas and, for the Cilium policies, the CRD catalog. It needs `kustomize` and
`kubeconform` (`mise install` in the repository). CI job: `kustomize`.

**kind test** (`tests/run-kind-test.sh`): creates a kind cluster, loads the three images
under the tag `kind`, applies the overlay `tests/kind`, waits for the configure Job and the
Deployments, and runs `tests/smoketest.sh` through a port-forward on 38080. On a failure it
prints the pods, the events and the logs. `--keep` leaves the cluster running. CI job: `kind`.

| Overlay setting | Why |
|-----------------|-----|
| The `mariadb` and `redis` components, namespace `misp` | The smallest deployment that runs |
| Test values for `misp-db`, `misp-app`, `misp-admin`, `MISP_UUID` | The configure Job refuses the base's placeholders |
| A ReadWriteOnce attachments claim | kind's local-path storage offers no ReadWriteMany |

The images must exist locally as `ghcr.io/oivindoh/misp-container{,-caddy,-modules}:${MISP_IMAGE_TAG:-2.5.37}`.
With podman, kind runs on the machine's rootful connection (`KIND_PODMAN_CONNECTION`, default
`podman-machine-default-root`), because a kind node needs privileges that rootless podman
does not give it. kind copies `HTTP_PROXY` and `HTTPS_PROXY` into the node, so a proxy on
localhost breaks the image pulls there.

## Hub-spoke sync test

**20 tests** verifying MISP server-to-server synchronization across 3 isolated instances.

**Stack:** 3 MISP instances (A, B, C), each with dedicated MySQL + Redis + web + caddy + worker. 18 containers total.

```
  A (spoke :18091)      B (hub :18092)       C (spoke :18093)
  +------------+        +------------+       +------------+
  | Event: A   |--pull->| Events:    |<-pull-| Event: C   |
  |            |        |  A, B, C   |       |            |
  +------------+        +------------+       +------------+
        ^                  |     |                  ^
        |                  |     |                  |
        +--pull tag:A------+     +-----pull tag:C---+
        +--push tag:A------+
```

| Phase | Tests | What it verifies |
|-------|-------|------------------|
| Setup | 2 | All 3 instances ready, configured via sync container |
| Pull (unfiltered) | 2 | B pulls events from A and C (1 event each) |
| Tagging | 2 | Events on B tagged with release-to:A and release-to:C |
| Pull (tag-filtered) | 4 | A gets only release-to:A events, C gets only release-to:C events |
| Push (tag-filtered) | 2 | B pushes tagged event to A, untagged event stays on B |
| Hub layout | 8 | A and C hold no active servers; B pulls from both, then pushes each spoke only its own tagged event |

Pulls and pushes are POSTs; a refused call fails the suite. Before each check the suite waits
until no job on the instance is unfinished.

**Files:**
- `run-sync-test.sh` -- test script
- `docker-compose.sync-test.yml` -- standalone 3-instance compose (not an overlay)
- `sync-test-{a,b,c}.env` -- per-instance env (MySQL host, Redis host, BASE_URL)
- `sync-test-secrets.env` -- shared secrets

Run with: `mise run test-sync`

## Migration suite

`run-migration-tests.sh` starts the integration stack on MariaDB, seeds it (an org with a
user, a sync user with a known authkey, a sync server, an event with an attachment) and
records the row counts. It then runs the migrate Job into a second MariaDB and checks the
refusals (a non-empty target, an identity mismatch, `MIGRATE_FORCE`), points the stack at the
copy and checks it: row counts, both authkeys, the org, the server, the event, the attachment
download, the fixture attachment copied from the mounted source files, `MISP.live` set by the
configure step, and the run in the sync log. The same copy and checks then run onto
PostgreSQL, plus the id sequences.

**Files:**
- `run-migration-tests.sh` -- test script
- `docker-compose.migrate.yml` -- overlay: the second MariaDB (`mysql-target`) and the migrate service with the fixture files mounted
- `docker-compose.migrate-mysql.yml`, `migrate-target-mysql.env` -- point the stack at `mysql-target` after the copy
- `migrate-orgs.yaml` -- seed content for the org sync

Run with: `mise run test-migration`

## CI

GitHub Actions on every push to master and every PR:

| Job | Runs |
|-----|------|
| `build` | The three images into the layer cache |
| `unit` | The unit tests |
| `integration` | This suite on MariaDB, against the images from `build` (`MISP_IMAGE_TAG=ci`) |
| `integration-postgres` | This suite on PostgreSQL, in parallel |
| `hub-spoke` | The sync suite, in parallel with `integration` |
| `migration` | The migration suite, in parallel |
| `scan` | Trivy on the three images |
| `release` | On a tag: push the images and create the GitHub Release, after every other job |

## Environment

All tests require podman with a compose provider (`podman compose`). Set `COMPOSE_CMD` and `CONTAINER_CMD` to use another runner, as CI does with `docker compose` and `docker`. Unit tests additionally need Python 3.13 + uv (managed by mise). The `mise.toml` auto-creates a venv on `cd` into the repo.
