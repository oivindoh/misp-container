# Testing

## TL;DR

Unit tests on the Python library, an upstream guard that checks the MISP in the image against what this repository patches or depends on, and three stack suites in pytest (`tests/e2e/`): an integration suite on one Compose stack, a hub-spoke sync suite on three instances, and a migration suite that copies a seeded instance onto MariaDB and PostgreSQL. A smoke test checks any live MISP through its API. Two Kubernetes checks cover the Helm chart: every render validated against the schemas, and the chart installed and upgraded on a kind cluster with the smoke test.

```bash
mise run test                # unit tests (~0.2s)
mise run test-upstream       # the upstream guard in the image (~5s)
mise run test-integration    # the upstream guard, then single-instance integration tests (~2min)
mise run test-integration -- --db-engine postgres   # the same on PostgreSQL
mise run test-sync           # hub-spoke sync test with 3 instances (~2min)
mise run test-migration      # the migration: MariaDB to MariaDB, MariaDB to PostgreSQL (~3min)
mise run smoketest https://misp.example.com   # a live MISP; asks for an API key
mise run test-all            # every suite
mise run test-chart          # helm lint, render the chart with each component, validate against the schemas (~10s)
mise run test-kind           # the chart on a kind cluster: install, helm test, task runs, upgrade, rollback (build the images first)
```

The stack suites build the images with compose, start full MISP stacks, and tear them down. Pass `-- --skip-build` to reuse existing images and `-- --keep` to leave the stack up.

## Unit tests

Tests of the Python entrypoint library (`files/misp_container/`), the scripts and the compose files.

| File | What it tests |
|------|---------------|
| `test_config.py` | Settings diff engine, version comparison, YAML loading, env var expansion, settings cache |
| `test_env.py` | Environment variable defaults, `apply_defaults()`, worker config derivation, derived variables |
| `test_task.py` | Periodic task runner: API tasks, index tasks, the workflow task, console tasks, the backlog guard |
| `test_scheduler_coverage.py` | The guard that fails when MISP's scheduler offers work no task covers |
| `test_check_upstream.py` | The upstream guard: every check on a MISP tree in the 2.5.47 shape, then on that tree changed the way a release could. The PHP run needs `php` and skips without it |
| `test_log.py`, `test_logrelay.py` | The JSON and text log formats; the relay of MISP's log files and their size cap |
| `test_metrics.py` | Metrics exporter: database metrics, the job queues in Redis (RESP client), network probes |
| `test_db.py` | Advisory lock holds one connection until release, statement splitter, the version gate |
| `test_engine.py` | Engine selection, SQL fragments per engine, `MYSQL_*` aliases, housekeeping batches, cursor handling |
| `test_configure.py` | Placeholder and identity checks of the configure step |
| `test_config_php.py` | config.php rendering from settings.yaml, PHP escaping and typing |
| `test_init.py` | app/Config rendering, database.php/email.php generation, GPG key import, writable check |
| `test_admin.py` | SQL escape function |
| `test_s3.py` | The migrate Job's S3 client: AWS's Signature Version 4 examples, path-style and virtual-hosted requests, paged listing, errors |
| `test_docs.py` | Every name a doc gives exists in the code, the complete lists are complete, the values quoted in sentences match the manifests, and the generated regions are current |
| `test_agents_md.py` | `AGENTS.md` is what `scripts/generate_agents_md.py` makes of the tree |
| `test_compose_files.py` | The test overlays repeat the env files they override, so podman-compose and docker compose start each service with the same settings |
| `test_sync.py` | Org sync engine: config normalization, merge logic, UUID validation, env expansion, role/org/tag/user/server/taxonomy/warninglist/sharing group apply logic, build rules (pull vs push tag format), allow_external user placement, default_role, disable unmanaged resources, full orchestrator flow |

Run with: `mise run test` or `PYTHONPATH=files python -m pytest tests/ -v`

## Upstream guard

`e2e/test_upstream.py` runs `scripts/check_upstream.py` in one container of the web image,
with the repository mounted, and reports each check as one test. A check reads MISP's own
files and fails when MISP changed something this repository patches or depends on: the job
queues, the Redis keys of the jobs, the scheduler's tasks, the API routes the image calls,
the keys MISP gives attachments, what `bootstrap.php` loads and logs to, and the files MISP
writes under `app/tmp/logs`. The
last check runs the logging block that `init.py` renders through PHP, in both formats. A
failure names the change, the MISP file and the file of ours to revisit; DEVELOPING.md lists
the checks. No stack starts, so the guard reports even when MISP no longer starts.

Run with: `mise run test-upstream`. `mise run test-integration` and the CI job
`integration` run it before the integration suite.

## Integration tests

The full MISP stack in Compose, checked feature by feature.

**Stack:** 1 MISP instance (configure + web x2 + caddy + worker + MariaDB or PostgreSQL + Redis + Garage S3 + dex)

| Suite | What it verifies |
|-------|------------------|
| Non-root operation | All containers run as UID 1000 |
| HTTP / Caddy | Login page, static CSS, root redirect |
| Admin user config | Email, password (no forced reset), org name, org UUID, last_pw_change |
| Database settings | DB persistence, setting count, BASE_URL in DB |
| Workers | The five queues run under supervisord, no scheduler program, web reaches supervisord over TCP |
| Background jobs | Event publish triggers job, worker completes it (status=4) |
| PHP-FPM | Listening on port 9002 |
| Distribution files and app/Config | taxonomies in the image, bootstrap.php is MISP's default plus the logging block, database.php host, config.php content |
| GPG | Auto-generated key in .gnupg volume |
| MISP API | Version endpoint, event create via API |
| Warm restart | Settings cache reload, minimum_config unchanged |
| Version-gated defaults | Defaults version saved, version gate stability, envar precedence |
| S3 attachment storage | Garage S3 bootstrap, upload, download, bucket verification |
| Custom auth | Header login (200), no-header redirect (302) |
| OIDC login | Redirect to dex, its form, the callback, the user's email, role by name, default organisation, mixed auth |
| Task runner | API tasks, the index tasks without items, the console tasks `periodic-summary` and `check-user-validity`, a missing `ADMIN_KEY` |
| Org sync | Org/user/tag/server creation, server authkey (DB verify), sync user authkey prefix, taxonomy enable, disabled user, custom warninglist create/update, warm run idempotency |
| Metrics exporter | Endpoints, core metrics, no scrape errors, a waiting job in `misp_jobs_queued` and back to 0, `misp_scheduled_tasks_enabled` |
| MISP modules | Enrichment through the modules service |
| Logging | JSON lines from configure and none in text, MISP's own log in the web and worker output once, no CakeLog files on disk, the relay forwarding `server-sync.log`, every log file MISP wrote in web and worker relayed |
| Multi-replica web | configure service ran once, two web replicas serve without configuring |
| Settings | Every MISP setting curated or catalogued, no rejected `cake` setting |

**Files:**
- `e2e/test_integration.py` -- the suite, in the order above; later sections use the state earlier ones leave
- `docker-compose.test.yml` -- overlay on `deploy/docker-compose.yml` (test ports, env, Garage S3, dex)
- `docker-compose.postgres.yml`, `postgres.env` -- second overlay for `--db-engine postgres`: points every MISP container at the postgres service
- `dex.yaml` -- the OIDC provider's static client and user
- `test-compose.env` -- test env overrides (BASE_URL, ADMIN_EMAIL, etc.)
- `test-compose-secrets.env` -- test secrets (passwords, Redis key), layered over `deploy/chart/files/secrets-*.env`
- `garage.toml` -- Garage S3 config for attachment testing

Run with: `mise run test-integration`

## Kubernetes checks

**Render check** (`scripts/check-chart.sh`): runs `helm lint --strict`, renders the chart with
the default values, with each component on, with every component on, and with supplied
Secrets, attachments in S3 and a workflow CronJob. It validates each render with
`kubeconform -strict` against the Kubernetes schemas and, for the Cilium policies and the
HTTPRoute, the CRD catalog. It needs `helm` and `kubeconform` (`mise install` in the
repository). CI job: `chart`. The unit tests (`test_chart.py`) check what the renders hold.

**kind test** (`tests/run-kind-test.sh`): creates a kind cluster, loads the three images
under the tag `kind`, and installs the chart with `tests/kind/values.yaml`. Each step must pass:

1. The configure Job and the Deployments finish; the smoke test (`e2e/test_smoke.py`) passes
   through a port-forward on 38080, and so does the chart's `helm test`.
2. An API task (`update-noticelists`), a console task (`periodic-summary`) and a housekeeping
   task (`housekeeping-jobs`), each started from its CronJob, succeed.
3. An upgrade with a changed value runs `configure-2`, removes `configure-1` and rolls the web
   pods; the smoke test passes.
4. `helm rollback` to revision 1 runs `configure-1` again, removes `configure-2`, restores the
   value and rolls the web pods; the smoke test and `helm test` pass.

On a failure it prints the pods, the events and the logs. `--keep` leaves the cluster
running. CI job: `kind`.

| Value | Why |
|-------|-----|
| The `mariadb` and `redis` components, namespace `misp` | The smallest deployment that runs |
| The `cronjobs` and `housekeeping` components | The task runs start from their CronJobs |
| Test values for `misp-db`, `misp-app`, `misp-admin`, `MISP_UUID` | The configure Job refuses the chart's placeholders |
| A ReadWriteOnce attachments claim | kind's local-path storage offers no ReadWriteMany |

The images must exist locally as `ghcr.io/oivindoh/misp-container{,-caddy,-modules}:${MISP_IMAGE_TAG}`; the tag defaults to the chart's `appVersion`, as the Compose files do.
With podman, kind runs on the machine's rootful connection (`KIND_PODMAN_CONNECTION`, default
`podman-machine-default-root`), because a kind node needs privileges that rootless podman
does not give it. kind copies `HTTP_PROXY` and `HTTPS_PROXY` into the node, so a proxy on
localhost breaks the image pulls there.

## Hub-spoke sync test

MISP server-to-server synchronisation across 3 isolated instances.

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

| Phase | What it verifies |
|-------|------------------|
| Setup | All 3 instances ready, configured via sync container |
| Pull (unfiltered) | B pulls events from A and C (1 event each) |
| Tagging | Events on B tagged with release-to:A and release-to:C |
| Pull (tag-filtered) | A gets only release-to:A events, C gets only release-to:C events |
| Push (tag-filtered) | B pushes tagged event to A, untagged event stays on B |
| Hub layout | A and C hold no active servers; B pulls from both, then pushes each spoke only its own tagged event |

Pulls and pushes are POSTs; a refused call fails the suite. Before each check the suite waits
until no job on the instance is unfinished.

**Files:**
- `e2e/test_sync.py` -- the suite; one module fixture per phase
- `docker-compose.sync-test.yml` -- standalone 3-instance compose (not an overlay)
- `sync-test-{a,b,c}.env` -- per-instance env (MySQL host, Redis host, BASE_URL)
- `sync-test-secrets.env` -- shared secrets

Run with: `mise run test-sync`

## Migration suite

`e2e/test_migration.py` starts the integration stack on MariaDB, seeds it (an org with a
user, a sync user with a known authkey, a sync server, an event with an attachment) and
records the row counts. The mounted source files hold an attachment in each place MISP keeps
one on disk: flat, under `bucket_<n>/` and under `shadow/`. The suite then runs the migrate Job
into a second MariaDB and checks the refusals (a non-empty target, an identity mismatch,
`MIGRATE_FORCE` alone on the Job's own copy, then with `MIGRATE_REPLACE_COPY`), points the stack at the copy and checks it: row counts, both authkeys, the
org, the server, the event, the attachment download, the fixture attachments under MISP's
keys, `MISP.live` set by the configure step, and the run in the sync log. The same copy and
checks then run onto PostgreSQL with the attachments uploaded into an S3 bucket in garage,
plus the id sequences. One parametrized list of checks runs against both copies. A last run
copies that bucket into a second one and compares every key and byte.

**Files:**
- `e2e/test_migration.py` -- the suite; module fixtures carry each step's state to the next
- `e2e/stack.py`, `e2e/conftest.py` -- the stack helper and the options every pytest suite shares (see below)
- `docker-compose.migrate.yml` -- overlay: the second MariaDB (`mysql-target`) and the migrate service with the fixture files mounted
- `docker-compose.migrate-mysql.yml`, `migrate-target-mysql.env` -- point the stack at `mysql-target` after the copy
- `docker-compose.migrate-s3.yml`, `migrate-target-s3.env` -- point the stack at `postgres` with the attachments in the bucket `misp-migrated`, after the copy
- `e2e/garage.py` -- layout, keys and buckets in the test stack's garage
- `migrate-orgs.yaml` -- seed content for the org sync

Run with: `mise run test-migration` (`-- --skip-build`, `-- --keep`)

## Smoke test

`e2e/test_smoke.py` checks a live MISP through its API: the login page, API auth,
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
| `conftest.py` | `--skip-build`, `--keep` (leave the stack up), `--db-engine`, `--url` and `--key` for the smoke test, a fixture that tells a module whether one of its tests failed, and the log dump at a module's first failure |

A suite is one module. Module fixtures run each step once and hand its result on; the tests
run in file order. At a module's first failure every service's log goes to
`$TMPDIR/<suite>-first-failure-compose-logs.txt`, and after a failure the teardown writes
`$TMPDIR/<suite>-compose-logs.txt`. When MISP never answers while a stack starts, before any
test runs, the logs go to `$TMPDIR/misp-wait-timeout-compose-logs.txt`. CI uploads them all with
the JUnit report. The first dump keeps the log of a container that a later step removes, such
as a web replica.

The suites behave the same under podman-compose, locally, and docker compose, in CI:

- **Read-only containers.** podman mounts a writable tmpfs on `/tmp`, `/run` and `/var/tmp`
  of a read-only container; docker and Kubernetes do not. Under podman the `Stack` passes
  `--read-only-tmpfs=false`, so a write there fails locally as it fails in CI and on a cluster.
- **Env files.** An overlay's `env_file` list repeats the list it overrides, then adds its own
  files. podman-compose appends an override list; docker compose merges it and drops repeats.
  Only a repeated prefix gives both the same settings; `test_compose_files.py` fails on a list
  that breaks the rule.

## CI

GitHub Actions on every push to master and every PR:

| Job | Runs |
|-----|------|
| `gate` | Stops a master run whose commit carries a release tag, since the tag run tests and releases that commit |
| `build` | The three images into the layer cache |
| `unit` | The unit tests |
| `integration` | The upstream guard, then the integration suite on MariaDB, against the images from `build` (`MISP_IMAGE_TAG=ci`); uploads the JUnit report and, after a failure, the compose logs |
| `integration-postgres` | The same on PostgreSQL, in parallel |
| `hub-spoke` | The sync suite, in parallel with `integration` |
| `migration` | The migration suite (pytest), in parallel; uploads the JUnit report and, after a failure, the compose logs |
| `chart` | `helm lint` and every chart render validated against the schemas |
| `kind` | The chart on a kind cluster: install, smoke test, `helm test`, task runs, upgrade, rollback |
| `scan` | Trivy on the three images |
| `release` | On a tag: push the images and the chart, and create the GitHub Release, after every other job |

## Environment

All tests require podman with a compose provider (`podman compose`). Set `COMPOSE_CMD` and `CONTAINER_CMD` to use another runner, as CI does with `docker compose` and `docker`. Unit tests also need Python 3.13, uv and helm, which mise installs. The `mise.toml` auto-creates a venv on `cd` into the repo.
