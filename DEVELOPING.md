# Developing

## Setup

[mise](https://mise.jdx.dev/) manages Python, uv, and the virtualenv automatically:

```bash
cd misp-container      # mise creates .venv on enter
mise run test          # unit tests (~0.3s)
mise run test-integration  # full Compose stack with podman (~90s)
mise run test-sync     # hub-spoke 3-instance sync (~60s)
mise run test-all      # unit + integration
```

## Tests

| Suite | Tests | What it covers |
|-------|-------|----------------|
| Unit | 243 | Config engine, config.php rendering, advisory lock, app/Config preparation, task runner, sync engine, metrics exporter |
| Integration | 104 | Full Compose stack: HTTP, auth, settings, PHP-FPM, workers, S3, org sync, metrics, modules enrichment |
| Hub-spoke sync | 12 | 3 isolated MISP instances: pull, push, tag-filtered sync |

Integration tests run all containers with `read_only: true` (except web, which needs to patch settings.yaml for version-gate tests) to catch filesystem write issues before they hit Kubernetes.

## New MISP releases

`.github/workflows/track-misp-releases.yaml` checks upstream daily. For a new release it
bumps `CORE_TAG`, resolves `files/composer.lock` for that release, builds the image, starts
the Compose stack, regenerates the settings catalogue from that MISP, and opens a PR with
all three changes. CI then builds, scans and runs
every suite on the PR, with the strict settings check and the rejected-`cake` check as the
early warning for changed settings and defaults. Review the catalogue diff in the PR: a
setting that appears there with a value this image should enforce moves to `settings.yaml`.

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
5. Updates image tags in `deploy/base/kustomization.yaml`
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

All MISP settings are defined in `files/misp-config/settings.yaml`. Each setting has a group, type, and value.

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
    db.py                   # MySQL queries via pymysql
    env.py                  # Environment variable defaults
    init.py                 # Per-pod preparation: app/Config rendering, GPG key import
    configure.py            # One-shot configuration (configure Job)
    log.py                  # Logging setup
    metrics.py              # Prometheus metrics collection
    sync.py                 # Declarative org/team/server sync engine
  misp-config/
    settings.yaml           # Curated defaults this image applies
    settings-upstream.yaml  # Generated catalogue of every other MISP setting (track_only)
scripts/
  update-settings.sh        # Regenerates the catalogue from a live stack
  update_settings.py        # The catalogue tool (--check in the integration suite)
  update-composer-lock.sh   # Resolves files/composer.lock through the composer-lock stage
  entrypoint-configure.py   # Configure Job entrypoint
  entrypoint-web.py         # PHP-FPM entrypoint
  entrypoint-worker.py      # Worker/scheduler entrypoint
  entrypoint-sync.py        # Org sync entrypoint (org-sync Job)
  entrypoint-metrics.py     # Prometheus metrics HTTP server (metrics Deployment)
  Caddyfile                 # Caddy configuration
  requirements-*.txt        # Pinned Python dependencies (final, modules)
  composer.lock             # Resolved PHP dependencies for the current CORE_TAG (generated)
tests/
  test_config.py            # Unit tests for settings engine
  test_env.py               # Unit tests for env handling
  test_init.py              # Unit tests for file operations
  test_sync.py              # Unit tests for sync engine
  test_metrics.py           # Unit tests for metrics exporter
  run-integration-tests.sh  # Containerised integration test suite
  run-sync-test.sh          # Hub-spoke 3-instance sync tests
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
