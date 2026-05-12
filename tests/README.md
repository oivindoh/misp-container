# Testing

Three test suites verify the MISP container image at different levels.

## Quick reference

```bash
mise run test                # unit tests (~0.2s)
mise run test-integration    # single-instance integration tests (~70s)
mise run test-sync           # hub-spoke sync test with 3 instances (~90s)
mise run test-all            # unit + integration (not sync)
```

All integration tests build Docker images, start full MISP stacks, and tear them down automatically. Pass `--skip-build` to reuse existing images.

## Unit tests

**153 tests** covering the Python entrypoint library (`files/misp_container/`).

| File | What it tests |
|------|---------------|
| `test_config.py` | Settings diff engine, version comparison, YAML loading, env var expansion, settings cache |
| `test_env.py` | Environment variable defaults, `apply_defaults()`, worker config derivation |
| `test_init.py` | File copy (no-clobber), make-writable, database.php/email.php generation |
| `test_admin.py` | SQL escape function |
| `test_sync.py` | Org sync engine: config normalization, merge logic, UUID validation, env expansion, role/org/tag/user/server/taxonomy/warninglist/sharing group apply logic, build rules (pull vs push tag format), allow_external user placement, default_role, disable unmanaged resources, full orchestrator flow |

Run with: `mise run test` or `PYTHONPATH=files python -m pytest tests/ -v`

## Integration tests

**59 tests** verifying the full MISP stack in Docker Compose.

**Stack:** 1 MISP instance (web + caddy + worker + init + MySQL + Redis + Garage S3)

| Suite | Tests | What it verifies |
|-------|-------|------------------|
| Non-root operation | 3 | All containers run as UID 1000 |
| HTTP / Caddy | 3 | Login page, static CSS, root redirect |
| Admin user config | 5 | Email, password (no forced reset), org name, org UUID, last_pw_change |
| Database settings | 3 | DB persistence, setting count, BASE_URL in DB |
| Workers | 6 | All supervisor queues running (default, prio, email, cache, update, scheduler) |
| Background jobs | 1 | Event publish triggers job, worker completes it (status=4) |
| PHP-FPM | 1 | Listening on port 9002 |
| Init container | 5 | VERSION file, taxonomies populated, bootstrap.php patch, database.php host |
| GPG | 1 | Auto-generated key in .gnupg volume |
| MISP API | 2 | Version endpoint, event create via API |
| Warm restart | 2 | Settings cache reload, minimum_config unchanged |
| Version-gated defaults | 3 | Defaults version saved, version gate stability, envar precedence |
| S3 attachment storage | 4 | Garage S3 bootstrap, upload, download, bucket verification |
| Custom auth | 2 | Header login (200), no-header redirect (302) |
| Org sync | 11 | Org/user/tag/server creation, server authkey (DB verify), sync user authkey prefix, taxonomy enable, disabled user, custom warninglist create/update, warm run idempotency |

**Files:**
- `run-integration-tests.sh` -- test script
- `docker-compose.test.yml` -- overlay on `deploy/docker-compose.yml` (test ports, env, Garage S3)
- `test-compose.env` -- test env overrides (BASE_URL, ADMIN_EMAIL, etc.)
- `test-compose-secrets.env` -- test secrets (passwords, Redis key)
- `garage.toml` -- Garage S3 config for attachment testing

Run with: `mise run test-integration`

## Hub-spoke sync test

**12 tests** verifying MISP server-to-server synchronization across 3 isolated instances.

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

**Files:**
- `run-sync-test.sh` -- test script
- `docker-compose.sync-test.yml` -- standalone 3-instance compose (not an overlay)
- `sync-test-{a,b,c}.env` -- per-instance env (MySQL host, Redis host, BASE_URL)
- `sync-test-secrets.env` -- shared secrets

Run with: `mise run test-sync`

## CI

GitHub Actions runs all three test suites on every push to master and on PRs:
1. Unit tests
2. Integration tests (single instance, ~70s)
3. Hub-spoke sync tests (3 instances, ~90s)

Trivy scans all three images (misp, caddy, sync) in parallel after tests pass.

## Environment

All tests require Docker and Docker Compose. Unit tests additionally need Python 3.13 + uv (managed by mise). The `mise.toml` auto-creates a venv on `cd` into the repo.
