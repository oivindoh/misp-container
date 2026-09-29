# Developing

## TL;DR

- `mise run test` for the unit tests, `mise run test-integration` for the Compose suite,
  `mise run test-sync` for the three-instance sync suite. Podman runs the containers.
- Settings: curated defaults in `files/misp-config/settings.yaml`, the generated catalogue in
  `settings-upstream.yaml` (`mise run settings-update`).
- New MISP release: the tracking workflow opens a PR with the bump, `files/composer.lock` and
  the catalogue; CI's strict checks tell you what changed.
- Release: `mise run release [vX.Y.Z]`, then push master and the tag.

## Setup

[mise](https://mise.jdx.dev/) manages Python, uv, and the virtualenv automatically:

```bash
cd misp-container      # mise creates .venv on enter
mise run test          # unit tests (~0.3s)
mise run test-integration  # full Compose stack with podman (~90s)
mise run test-integration -- --postgres   # the same suite on PostgreSQL
mise run test-sync     # hub-spoke 3-instance sync (~60s)
mise run test-all      # unit + integration + sync
```

## Tests

| Suite | Tests | What it covers |
|-------|-------|----------------|
| Unit | 271 | Config engine, config.php rendering, advisory lock, database helpers, app/Config preparation, task runner, sync engine, metrics exporter |
| Integration | 111 | Full Compose stack: HTTP, auth, settings, PHP-FPM, workers, S3, org sync, metrics, modules enrichment |
| Hub-spoke sync | 12 | 3 isolated MISP instances: pull, push, tag-filtered sync |
| Migration | 48 | The migrate Job: a seeded MariaDB copied onto a second MariaDB and onto PostgreSQL, refusals, the copy checked through the API |

The integration suite runs every container with `read_only: true` (except web, whose version-gate checks patch `settings.yaml` in place) to catch filesystem writes before Kubernetes does, and prints the wall time of each section. Set `COMPOSE_CMD` and `CONTAINER_CMD` for another runner; CI uses `docker compose` and `docker`.

## New MISP releases

`.github/workflows/track-misp-releases.yaml` checks upstream daily. For a new release it
bumps `CORE_TAG`, resolves `files/composer.lock` for that release, builds the image, starts
the Compose stack, regenerates the settings catalogue from that MISP, and opens a PR with
all three changes. CI then builds, scans and runs
every suite on the PR, with the strict settings check and the rejected-`cake` check as the
early warning for changed settings and defaults, and the scheduler coverage check
(`scripts/check_scheduler_coverage.py`) for new periodic work. When MISP's scheduler offers a
task type, action or admin action that no task covers, that check fails: add the task to
`files/misp_container/task.py` (`SCHEDULER_COVERAGE`) and a CronJob to the `cronjobs`
component. The PR body lists the new settings by level. A
setting that appears there with a value this image should enforce moves to `settings.yaml`;
when a release changes a secure default we already curate, give it `since: <that tag>` so
existing instances pick the new value up once.

Upstream source is patched in two places. `init.py` patches `bootstrap.php` with the auth
plugin detection when it renders `app/Config`. The `Dockerfile` patches CakePHP's
`Postgres.php` (`describe()` resets its sequence match per column), guarded by a `grep` that
fails the build when the patched line changes. On a failed guard, check whether the release
carries the fix and drop the patch, or adapt it.

## Releases

Image tags match the MISP version. Tags are immutable -- CI refuses to overwrite an existing tag.

### Using `mise run release`

The release task automates version bumping, image tag updates, and git tagging:

```bash
# Hotfix (same MISP version, increments -rN suffix)
mise run release
# v2.5.37-r3 exists -> creates v2.5.37-r4

# New upstream MISP version
mise run release v2.5.38
# Updates Dockerfile ARG CORE_TAG, creates v2.5.38 tag
```

The task:
1. Reads the current `CORE_TAG` from the Dockerfile
2. Optionally updates it if a new upstream tag is provided
3. Checks origin for existing release tags
4. Computes the next tag (`v2.5.38` or `v2.5.37-rN+1`)
5. Updates the image tags in `deploy/base/kustomization.yaml`, the components that pin the image, the Compose files (`MISP_IMAGE_TAG` default) and, if present, the `?ref=` pins of the local ArgoCD overlay (not committed)
6. Shows the diff and asks for confirmation
7. Commits and creates the git tag

After confirming:
```bash
git push origin master <tag>
```

CI runs tests, scans, pushes images, and creates a GitHub Release with:
- `ghcr.io/oivindoh/misp-container:<version>`
- `ghcr.io/oivindoh/misp-container-caddy:<version>`
- `ghcr.io/oivindoh/misp-container-modules:<version>`

### Tag format

| Tag | Meaning |
|-----|---------|
| `v2.5.38` | First release tracking MISP v2.5.38 |
| `v2.5.38-r1` | Hotfix rebuild (base image update, config fix, security patch) |
| `v2.5.38-r2` | Second hotfix |

## Settings Engine

`files/misp-config/settings.yaml` holds the defaults this image applies; each setting has a group and a value.

### Adding a setting

Every setting has a group and a default value. The env var that overrides it is derived from
its name (`MISP.my_setting` -> `MISP_MY_SETTING`); nothing else to declare.

```yaml
MISP.my_setting:
  group: optional
  value: some-default
```

- With the env var set and non-empty, the value is enforced on every configure run.
- Without it, the default is applied once when the setting is missing; the user then owns it
  through the MISP UI.

The YAML type of `value` (bool, int, string) is the type rendered into `config.php` for the
`minimum_config` and `db_enable` groups; env overrides are cast to it.

### Version-gated defaults

Re-apply a default when upgrading past a specific image version:

```yaml
MISP.my_setting:
  group: critical
  value: new-secure-value
  since: v2.5.40
```

The version gate only triggers once per image version. Env vars always take precedence.

### Optional fields

- `force: true` -- pass `-f` to `cake Admin setSetting`
- `blank_protection: true` -- skip if the value is empty (and leave the key out of `config.php`)
- `sensitive: true` -- redact the value in logs
- `since: v2.5.40` -- version-gated default

### The upstream catalogue

`files/misp-config/settings-upstream.yaml` is generated, never edited: every setting the
running MISP defines that `settings.yaml` does not name, with MISP's own default, description,
type and level, all `track_only`. The source is `cake Admin getSetting all` inside the web
container; the REST endpoint strips descriptions. The image never applies those values; the file documents
them and makes every MISP setting overridable through its derived env var.

```bash
mise run settings-update              # build the stack, regenerate, tear down
mise run settings-update -- --skip-build
mise run composer-lock                # resolve files/composer.lock for the current CORE_TAG
```

`files/composer.lock` pins MISP's PHP dependencies plus this image's extra packages. The
build installs exactly that lock and fails when it no longer matches upstream's
`composer.json`, which is the signal to run `mise run composer-lock` after a bump.

The integration suite runs `scripts/update_settings.py --check` and fails when MISP defines a
setting neither file names, or when `settings.yaml` names one MISP no longer defines. It also
fails when the configure step logs a rejected `cake setSetting`. To give a catalogued setting
a default of this image, move it into `settings.yaml`.

### Groups

| Group | Where it lands | Who applies it |
|-------|----------------|----------------|
| `minimum_config`, `db_enable` | `app/Config/config.php`, rendered from the YAML value type (bool, int, string) | Every entrypoint, in every pod, on every start |
| `s3` | `config.php` as well, when `PLUGIN_S3_BUCKET_NAME` is set | Every entrypoint |
| all other groups | the `system_settings` table, in order `initialisation` -> `critical` -> `optional` -> `upstream` -> `gpg` -> `s3` -> `proxy` | The configure Job |
| `upstream` (generated) | only env overrides; defaults are never written | The configure Job |

MISP never reads `SystemSetting::BLOCKED_SETTINGS` (salt, encryption key, password policy,
`python_bin`, `ca_path`, `tmpdir`, `attachments_dir`, `system_setting_db` and a few more) from
the database. Those settings must stay in `minimum_config`.

## Project Structure

```
files/
  misp_container/           # Python entrypoint library
    __init__.py
    admin.py                # Admin user, GPG, auth configuration
    api.py                  # MISP REST API client (urllib)
    cake.py                 # CakePHP CLI wrapper
    config.py               # Settings diff engine (SettingSpec, SettingsCache)
    db.py                   # Engine-neutral database layer (pymysql or pg8000), lock, schema import
    housekeeping.py         # Nightly deletes (housekeeping component)
    migrate.py              # Copy of an existing MySQL/MariaDB MISP database (migrate component)
    env.py                  # Environment variable defaults
    init.py                 # Per-pod preparation: app/Config rendering, GPG key import
    configure.py            # One-shot configuration (configure Job)
    log.py                  # Logging setup
    metrics.py              # Prometheus metrics collection
    sync.py                 # Declarative org/team/server sync engine
    task.py                 # Periodic task runner (cronjobs component)
  misp-config/
    settings.yaml           # Curated defaults this image applies
    settings-upstream.yaml  # Generated catalogue of every other MISP setting (track_only)
  entrypoint-configure.py   # Configure Job entrypoint
  entrypoint-web.py         # PHP-FPM entrypoint
  entrypoint-worker.py      # Worker entrypoint
  entrypoint-sync.py        # Org sync entrypoint (org-sync Job)
  entrypoint-metrics.py     # Prometheus metrics HTTP server (metrics Deployment)
  Caddyfile                 # Caddy configuration
  php.ini.template, php-fpm-pool.conf.template
  requirements-*.txt        # Pinned Python dependencies (final, modules)
  composer.lock             # Resolved PHP dependencies for the current CORE_TAG (generated)
scripts/
  release.sh                # mise run release
  update-settings.sh        # Regenerates the catalogue from a live stack
  update_settings.py        # The catalogue tool (--check in the integration suite)
  check_scheduler_coverage.py  # Fails when MISP's scheduler offers work no task covers
  update-composer-lock.sh   # Resolves files/composer.lock through the composer-lock stage
tests/
  test_*.py                 # Unit tests (see tests/README.md)
  run-integration-tests.sh  # Containerised integration test suite
  run-sync-test.sh          # Hub-spoke 3-instance sync tests
  docker-compose.test.yml   # Test overlay on deploy/docker-compose.yml
  docker-compose.postgres.yml  # Second overlay for --postgres
  docker-compose.sync-test.yml
deploy/
  docker-compose.yml        # Local development stack (podman compose)
  base/                     # Kustomize base (MISP itself)
  components/               # Optional parts: database, cache, ingress, network policy, cronjobs, PDB
  overlays/                 # Kustomize overlays
argocd/
  application.yaml          # ArgoCD Application example
  overlay/                  # Remote-base kustomize overlay
docs/
  migration.md              # Migration guide from existing MISP
```
