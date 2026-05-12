# Developing

## Setup

[mise](https://mise.jdx.dev/) manages Python, uv, and the virtualenv automatically:

```bash
cd misp-container      # mise creates .venv on enter
mise run test          # unit tests (~0.3s)
mise run test-integration  # full Docker Compose stack (~90s)
mise run test-sync     # hub-spoke 3-instance sync (~60s)
mise run test-all      # unit + integration
```

## Tests

| Suite | Tests | What it covers |
|-------|-------|----------------|
| Unit | 193 | Config engine, init logic, sync engine, metrics exporter |
| Integration | ~90 | Full Compose stack: HTTP, auth, settings, PHP-FPM, workers, S3, org sync, metrics, modules enrichment |
| Hub-spoke sync | 12 | 3 isolated MISP instances: pull, push, tag-filtered sync |

Integration tests run all containers with `read_only: true` (except web, which needs to patch settings.yaml for version-gate tests) to catch filesystem write issues before they hit Kubernetes.

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
- `ghcr.io/oivindoh/misp-container-sync:<version>`
- `ghcr.io/oivindoh/misp-container-metrics:<version>`
- `ghcr.io/oivindoh/misp-container-modules:<version>`

### Tag format

| Tag | Meaning |
|-----|---------|
| `v2.5.38` | First release tracking MISP v2.5.38 |
| `v2.5.38-r1` | Hotfix rebuild (base image update, config fix, security patch) |
| `v2.5.38-r2` | Second hotfix |

## Settings Engine

All MISP settings are defined in `files/misp-config/settings.yaml`. Each setting has a group, type, and value.

### Adding a new enforced setting

```yaml
MISP.my_setting:
  group: optional
  type: envar
  value: "${MY_ENV_VAR}"
```

Then add `MY_ENV_VAR` to `files/misp_container/env.py` DEFAULTS dict.

### Adding a new default

```yaml
MISP.my_setting:
  group: optional
  type: default
  value: some-default
```

### Version-gated defaults

Re-apply a default when upgrading past a specific image version:

```yaml
MISP.my_setting:
  group: critical
  type: default
  value: new-secure-value
  since: v2.5.40
```

The version gate only triggers once per image version. Env vars always take precedence.

### Setting types

- **`type: envar`** -- enforced from environment variables on every startup. `${VAR}` is expanded at runtime. Only changed values trigger a `cake Admin setSetting` call.
- **`type: default`** -- applied once when the setting doesn't exist. User changes via the MISP UI are preserved.

### Groups

Applied in order: `minimum_config` -> `db_enable` -> `initialisation` -> `critical` -> `optional` -> `gpg` -> `s3` -> `proxy`.

The `minimum_config` group writes to `config.php` (bootstrap settings like Redis host, Python path). All other groups write to the `system_settings` table.

### Optional fields

- `force: true` -- pass `-f` to `cake Admin setSetting`
- `blank_protection: true` -- skip if the value is empty
- `since: v2.5.40` -- version-gated default

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
    init.py                 # Init container volume population
    log.py                  # Logging setup
    metrics.py              # Prometheus metrics collection
    sync.py                 # Declarative org/team/server sync engine
  misp-config/
    settings.yaml           # All MISP settings in one file
  entrypoint-init.py        # Init container entrypoint
  entrypoint-web.py         # PHP-FPM entrypoint
  entrypoint-worker.py      # Worker/scheduler entrypoint
  entrypoint-sync.py        # Org sync entrypoint
  entrypoint-metrics.py     # Prometheus metrics HTTP server
  Caddyfile                 # Caddy configuration
  requirements-*.txt        # Pinned Python dependencies per image
tests/
  test_config.py            # Unit tests for settings engine
  test_env.py               # Unit tests for env handling
  test_init.py              # Unit tests for file operations
  test_sync.py              # Unit tests for sync engine
  test_metrics.py           # Unit tests for metrics exporter
  run-integration-tests.sh  # Dockerized integration test suite
  run-sync-test.sh          # Hub-spoke 3-instance sync tests
deploy/
  docker-compose.yml        # Local development stack
  base/                     # Kustomize base
  overlays/                 # Kustomize overlays
argocd/
  application.yaml          # ArgoCD Application example
  overlay/                  # Remote-base kustomize overlay
docs/
  migration.md              # Migration guide from existing MISP
```
