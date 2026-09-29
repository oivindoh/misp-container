# Testing

## TL;DR

Unit tests on the Python library, and three stack suites in pytest (`tests/e2e/`): an integration suite on one Compose stack, a hub-spoke sync suite on three instances, and a migration suite that copies a seeded instance onto MariaDB and PostgreSQL. A smoke test checks any live MISP through its API. Two Kubernetes checks cover the Kustomize base: every render validated against the schemas, and the base applied to a kind cluster with the smoke test.

```bash
mise run test                # unit tests (~0.2s)
mise run test-integration    # single-instance integration tests (~2min)
mise run test-integration -- --db-engine postgres   # the same on PostgreSQL
mise run test-sync           # hub-spoke sync test with 3 instances (~2min)
mise run test-migration      # migrate Job: MariaDB to MariaDB, MariaDB to PostgreSQL (~3min)
mise run smoketest https://misp.example.com   # a live MISP; asks for an API key
mise run test-all            # every suite
mise run test-kustomize      # render the base with each component, validate against the schemas (~10s)
mise run test-kind           # the base on a kind cluster, then the smoke test (build the images first)
```

The stack suites build the images with compose, start full MISP stacks, and tear them down. Pass `-- --skip-build` to reuse existing images and `-- --keep` to leave the stack up.

## Unit tests

**344 tests** covering the Python entrypoint library (`files/misp_container/`) and the scripts.

| File | What it tests |
|------|---------------|
| `test_config.py` | Settings diff engine, version comparison, YAML loading, env var expansion, settings cache |
| `test_env.py` | Environment variable defaults, `apply_defaults()`, worker config derivation, derived variables |
| `test_task.py` | Periodic task runner: API tasks, index tasks, the workflow task, console tasks, the backlog guard |
| `test_scheduler_coverage.py` | The guard that fails when MISP's scheduler offers work no task covers |
| `test_log.py`, `test_logrelay.py` | The JSON and text log formats; the relay of MISP's log files and their size cap |
| `test_metrics.py` | Metrics exporter: database metrics, the job queues in Redis (RESP client), network probes |
| `test_db.py` | Advisory lock holds one connection until release, statement splitter, the version gate |
| `test_engine.py` | Engine selection, SQL fragments per engine, `MYSQL_*` aliases, housekeeping batches, cursor handling |
| `test_configure.py` | Placeholder and identity checks of the configure step |
| `test_config_php.py` | config.php rendering from settings.yaml, PHP escaping and typing |
| `test_init.py` | app/Config rendering, database.php/email.php generation, GPG key import, writable check |
| `test_admin.py` | SQL escape function |
| `test_sync.py` | Org sync engine: config normalization, merge logic, UUID validation, env expansion, role/org/tag/user/server/taxonomy/warninglist/sharing group apply logic, build rules (pull vs push tag format), allow_external user placement, default_role, disable unmanaged resources, full orchestrator flow |

Run with: `mise run test` or `PYTHONPATH=files python -m pytest tests/ -v`

## Integration tests

**131 tests** verifying the full MISP stack in Compose.

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
| Task runner | 13 | API tasks, the index tasks without items, the console tasks `periodic-summary` and `check-user-validity`, a missing `ADMIN_KEY` |
| Org sync | 18 | Org/user/tag/server creation, server authkey (DB verify), sync user authkey prefix, taxonomy enable, disabled user, custom warninglist create/update, warm run idempotency |
| Metrics exporter | 22 | Endpoints, core metrics, no scrape errors, a waiting job in `misp_jobs_queued` and back to 0, `misp_scheduled_tasks_enabled` |
| MISP modules | 6 | Enrichment through the modules service |
| Logging | 7 | JSON lines from configure and none in text, MISP's own log in the web and worker output once, no CakeLog files on disk, the relay forwarding `server-sync.log` |
| Multi-replica web | 6 | configure service ran once, two web replicas serve without configuring |
| Settings and scheduler coverage | 3 | Every MISP setting curated or catalogued, every scheduler task covered by a task, no rejected `cake` setting |

**Files:**
- `e2e/test_integration.py` -- the suite, in the order above; later sections use the state earlier ones leave
- `docker-compose.test.yml` -- overlay on `deploy/docker-compose.yml` (test ports, env, Garage S3, dex)
- `docker-compose.postgres.yml`, `postgres.env` -- second overlay for `--db-engine postgres`: points every MISP container at the postgres service
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
Deployments, and runs the smoke test (`e2e/test_smoke.py`) through a port-forward on 38080. On a failure it
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
- `e2e/test_sync.py` -- the suite; one module fixture per phase
- `docker-compose.sync-test.yml` -- standalone 3-instance compose (not an overlay)
- `sync-test-{a,b,c}.env` -- per-instance env (MySQL host, Redis host, BASE_URL)
- `sync-test-secrets.env` -- shared secrets

Run with: `mise run test-sync`

## Migration suite

`e2e/test_migration.py` (pytest, 48 tests) starts the integration stack on MariaDB, seeds it (an org with a
user, a sync user with a known authkey, a sync server, an event with an attachment) and
records the row counts. It then runs the migrate Job into a second MariaDB and checks the
refusals (a non-empty target, an identity mismatch, `MIGRATE_FORCE`), points the stack at the
copy and checks it: row counts, both authkeys, the org, the server, the event, the attachment
download, the fixture attachment copied from the mounted source files, `MISP.live` set by the
configure step, and the run in the sync log. The same copy and checks then run onto
PostgreSQL, plus the id sequences. One parametrized list of 17 checks runs against both copies.

**Files:**
- `e2e/test_migration.py` -- the suite; module fixtures carry each step's state to the next
- `e2e/stack.py`, `e2e/conftest.py` -- the stack helper and the options every pytest suite shares (see below)
- `docker-compose.migrate.yml` -- overlay: the second MariaDB (`mysql-target`) and the migrate service with the fixture files mounted
- `docker-compose.migrate-mysql.yml`, `migrate-target-mysql.env` -- point the stack at `mysql-target` after the copy
- `migrate-orgs.yaml` -- seed content for the org sync

Run with: `mise run test-migration` (`-- --skip-build`, `-- --keep`)

## Smoke test

`e2e/test_smoke.py` (17 tests) checks a live MISP through its API: the login page, API auth,
the version, `MISP.baseurl` and `MISP.live`, the org, event and user indexes, supervisord and
the five worker queues, the enrichment URL, and an event created and deleted. `--url` names
the MISP; without `--key` only the login page is checked; without `--url` the module skips.

Run with: `mise run smoketest https://misp.example.com`

## Stack suites in pytest

The stack suites and the smoke test are pytest modules in `tests/e2e/`. The unit run leaves
them out (`--ignore=tests/e2e`).

| Piece | Does |
|-------|------|
| `stack.py` | The `Stack` class: compose calls, `exec` and logs through the engine (a third of a compose call), SQL through the image's db layer, HTTP to MISP, waits, the log dump after a failure |
| `conftest.py` | `--skip-build`, `--keep` (leave the stack up), `--db-engine`, `--url` and `--key` for the smoke test, and a fixture that tells a module whether one of its tests failed |

A suite is one module. Module fixtures run each step once and hand its result on; the tests
run in file order. After a failure the teardown writes every service's log to
`$TMPDIR/<suite>-compose-logs.txt`, which CI uploads with the JUnit report.

## CI

GitHub Actions on every push to master and every PR:

| Job | Runs |
|-----|------|
| `build` | The three images into the layer cache |
| `unit` | The unit tests |
| `integration` | The integration suite on MariaDB, against the images from `build` (`MISP_IMAGE_TAG=ci`); uploads the JUnit report and, after a failure, the compose logs |
| `integration-postgres` | The same on PostgreSQL, in parallel |
| `hub-spoke` | The sync suite, in parallel with `integration` |
| `migration` | The migration suite (pytest), in parallel; uploads the JUnit report and, after a failure, the compose logs |
| `kustomize` | Every Kustomize render validated against the schemas |
| `kind` | The base on a kind cluster, then the smoke test |
| `scan` | Trivy on the three images |
| `release` | On a tag: push the images and create the GitHub Release, after every other job |

## Environment

All tests require podman with a compose provider (`podman compose`). Set `COMPOSE_CMD` and `CONTAINER_CMD` to use another runner, as CI does with `docker compose` and `docker`. Unit tests additionally need Python 3.13 + uv (managed by mise). The `mise.toml` auto-creates a venv on `cd` into the repo.
