# Developing

## TL;DR

- `mise run test` for the unit tests. `mise run build` builds the images, and each stack suite
  (`mise run test-integration` and the others) installs the chart on a k3s cluster in podman.
- Settings: curated defaults in `files/misp-config/settings.yaml`, the generated catalogue in
  `settings-upstream.yaml` (`mise run settings-update`).
- New MISP release: the tracking workflow opens a PR with the bump, `files/composer.lock`,
  `files/requirements-misp.txt` and the catalogue; CI's strict checks tell you what changed.
- MISP: the `Dockerfile` pins MISP's `3.0_wip` branch at a commit (see The MISP pin).
- Release: `mise run release` makes the next `3.0.0-wip.N`; push master and the tag.
- Pull requests: a draft runs the unit tests only; marking it ready for review runs every suite.
  A pull request merges once `ci-ok` passes on a tree up to date with master.

## Setup

[mise](https://mise.jdx.dev/) manages Python, uv, and the virtualenv automatically:

```bash
cd misp-container      # mise creates .venv on enter
mise run test          # unit tests, the chart lint and every chart render validated against the schemas
```

[tests/README.md](tests/README.md) lists every suite, its command and what it covers.

## New MISP releases

`.github/workflows/track-misp-releases.yaml` checks upstream daily. For a new release it
bumps `CORE_TAG`, resolves `files/composer.lock` and `files/requirements-misp.txt` for that
release, builds the images, installs the chart on k3s, regenerates the settings catalogue from
that MISP, and opens a PR with all four changes. CI then builds, scans and runs every suite on the PR. Three checks warn
about a change in MISP:

| Check | Fails when |
| --- | --- |
| The upstream guard: `scripts/check_upstream.py`, run in the image by `tests/e2e/test_upstream.py` | MISP changed something the image patches or depends on (table below) |
| The strict settings check: `scripts/update_settings.py --check` in the integration suite | MISP has a setting that neither `settings.yaml` nor the catalogue lists |
| The rejected-`cake` check in the integration suite | MISP refuses a default this image applies |

The PR body lists the new settings by level. A setting that appears there with a value this
image should enforce moves to `settings.yaml`. A default reaches new instances only; to change
the value on existing instances too, set its env var in `base.env`, which the configure Job
enforces on every run.

### What the image depends on in MISP

A failure of the upstream guard names the change, the MISP file and the file of ours to
revisit.

<!-- generated: upstream-checks -->
| Check | MISP file | Ours | Fails when MISP |
|---|---|---|---|
| `worker-queues` | app/Lib/Tools/BackgroundJobsTool.php | files/misp_container/__init__.py WORKER_QUEUES | adds or drops a job queue |
| `job-keys` | app/Lib/Tools/BackgroundJobsTool.php | files/misp_container/metrics.py _collect_queue_metrics() | keeps waiting or running jobs under other Redis keys |
| `scheduler-tasks` | app/Console/Command/SchedulerWorkerShell.php | files/misp_container/task.py SCHEDULER_COVERAGE | offers periodic work that no task covers |
| `console-commands` | app/Console/Command | files/misp_container/task.py TASKS | renames or drops a console command a task runs |
| `api-routes` | app/Controller | the file and line named in each problem | drops a controller or an action the image calls |
| `attachment-keys` | app/Lib/Tools/AttachmentTool.php, app/Lib/Tools/AWSS3Client.php | files/misp_container/migrate.py attachment_key(), files/misp_container/s3.py | keys attachments another way, or stops sending path-style S3 requests |
| `cakeresque` | app/Config/bootstrap.default.php | Dockerfile stage composer-prep | loads CakeResque while SimpleBackgroundJobs is on |
| `cakelog-streams` | app/Config/bootstrap.default.php | files/misp_container/init.py LOG_BLOCK | configures a CakeLog file stream that the logging block does not drop |
| `shell-streams` | app/Lib/cakephp/lib/Cake/Console/Shell.php | files/misp_container/init.py LOG_BLOCK | checks other stream names before it adds its console streams |
| `relayed-files` | app/Model, app/Lib, app/Controller, app/Console, app/Plugin | files/misp_container/logrelay.py FILES | writes a new file under app/tmp/logs, or stops writing one the relay follows |
| `image-paths` | app/Model/Server.php, app/View/Helper/OrgImgHelper.php | deploy/chart/templates/_helpers.tpl misp.appVolumeMounts | uploads org logos or custom images to, or reads them from, a directory the volumes do not mount |
| `python-requirements` | requirements.txt | files/requirements-misp.txt | needs a Python package, or a version of one, that the pins do not give (mise run misp-requirements) |
| `log-block-php` | php | files/misp_container/init.py LOG_BLOCK | cannot run the logging block, in either format |
<!-- end generated -->

A new queue needs its name in `WORKER_QUEUES` and `NUM_WORKERS_<QUEUE>` in
`deploy/chart/files/base.env`. New periodic work needs a task in `misp_container/task.py`
(`SCHEDULER_COVERAGE`) and a schedule in `cronjobs.tasks` in `deploy/chart/values.yaml`.

### Patches to MISP

Each change to MISP's code is a patch in `patches/`: a git diff against MISP's root, after a
header with a title, a `Why:` line and an `Upstream:` line. AGENTS.md lists them.
`scripts/apply_patches.py` applies them in name order in the image build. A patch that no
longer applies, or that MISP already carries, fails the build with its name: check whether
MISP fixed the problem, then delete the patch or adapt it to the new MISP.

| Task | Command |
|------|---------|
| Apply the patches to a MISP checkout | `python3 scripts/apply_patches.py <MISP checkout>` |
| Make a patch | Change the file in a MISP checkout, `git diff > patches/<NN>-<name>.patch`, then add the header on top |

## The MISP pin

The `Dockerfile` pins MISP's `3.0_wip` branch: `ARG CORE_TAG=3.0_wip`, and `ARG CORE_COMMIT`
names the commit. MISP has not released 3.0 yet; the tracking workflow opens the pull request
that moves the pin to `v3.0.0` once it does. The pin shapes the image in these places:

| Where | What | Because |
|-------|------|---------|
| `Dockerfile` | No `cake-resque` patch in `composer-prep`; submodules without recursion | MISP 3.0 requires no `iglocska/cake-resque`, and only `misp-iconify` nests submodules, over SSH URLs |
| The version marker | `3.0_wip+<CORE_COMMIT>` | It changes with each pin, so the pods wait for the configure run of their own build |
| `settings.yaml` | No switch for background jobs, and four other settings fewer | MISP 3.0 defines none of them; an `s3://` attachments directory turns S3 on |
| The configure step | Refuses a database whose `attributes` table has the text column `value1` | That is MISP 2.x's layout; the migrate Job imports such a database (docs/migration.md) |

### Moving to a newer 3.0_wip commit

MISP changes its schema one way, so a database configured by a newer pin cannot go back to an
older image. Each pin move is a release of its own.

1. On a branch from master, set `ARG CORE_COMMIT` in the `Dockerfile` to the commit.
2. Run `mise run composer-lock` and `mise run misp-requirements`.
3. Run `mise run settings-update`. Remove from `settings.yaml` each setting the summary names
   as stale.
4. Open a pull request to master. The upstream guard and the suites report what MISP changed.
5. After the merge, release it (see Releases).

## Releases

While MISP is a work-in-progress pin, every release is the next pre-release of the version the
pin prepares, the same for the git tag, the images and the chart: `v3.0.0-wip.1`, then
`v3.0.0-wip.2` and on. `mise run release` takes no kind then.

Once the pin is a MISP release tag such as `v3.0.0`, each release has a kind. The git tag and
the images carry the MISP version, with `-rN` from the second release of it on, and the chart
has its own SemVer version. The first release after the pre-releases takes their version
without the suffix.

| Kind | Command | Chart version | For |
|------|---------|---------------|-----|
| `normal` | `mise run release normal` | minor up: `3.0.2` to `3.1.0` | A new MISP version, new features |
| `hotfix` | `mise run release hotfix` | patch up: `3.0.2` to `3.0.3` | A base image update, a fix, a security patch |
| `breaking` | `mise run release breaking` | major up: `3.0.2` to `4.0.0` | A change that breaks existing values or the upgrade path |

`scripts/release.py`:
1. Refuses to run off master, with changes to tracked files, or out of step with origin.
2. Computes the next tag from the `Dockerfile` pin and the tags that exist.
3. For a kind, reads the chart version that the previous release published, from the
   `Chart.yaml` of its tag, and raises the part the kind names.
4. Sets `version` and `appVersion` in `deploy/chart/Chart.yaml`.
5. Shows the plan and the diff. On a yes it commits and tags; on a no it restores the files.

Publish the release. `--atomic` lands the branch and the tag together:

```bash
git push --atomic origin master <tag>
```

The push starts two runs of the release commit. The `gate` job stops the master run, and the
tag run tests the commit and releases it.

CI runs the tests and the scans, then pushes the images and the chart, and creates a GitHub
Release that names the MISP commit. The `publish` job refuses a tag whose image tag differs
from `appVersion`, an image tag that GHCR already holds, and a chart version that GHCR
already holds.

| Artefact | Version |
|----------|---------|
| `ghcr.io/oivindoh/misp-container`, `-caddy`, `-modules` | the image tag: the git tag without the `v` |
| `oci://ghcr.io/oivindoh/charts/misp` | the chart version of `Chart.yaml` |

Helm installs a pre-release chart only when the command names its version (`--version
3.0.0-wip.N`) or passes `--devel`.

## Merging

The ruleset "master requires CI" requires the check `ci-ok` on a pull request that is up to
date with master, before a merge into master. Repository admins bypass it. `ci-ok` fails when
any job of the run failed or was cancelled, and on a draft. The push run of a merge commit
skips `unit` and the suites, since its tree is the one the pull request tested. Rename
`ci-ok` in the ruleset as well: a required check that never reports holds every pull request
open.

## Settings Engine

`files/misp-config/settings.yaml` holds the defaults this image applies; each setting has a group and a value.

### Adding a setting

Every setting sits in a group and has a default value. The env var that overrides it is
derived from its name (`MISP.my_setting` -> `MISP_MY_SETTING`); nothing else to declare.
[docs/configuration.md](docs/configuration.md#settings) gives the rule for a set and an unset
env var.

```yaml
settings:
  optional:
    MISP.my_setting:
      value: some-default
```

The YAML type of `value` (bool, int, string) is the type rendered into `config.php` for the
`minimum_config` and `db_enable` groups; env overrides are cast to it.

### Optional fields

- `force: true` -- pass `-f` to `cake Admin setSetting`
- `blank_protection: true` -- skip if the value is empty (and leave the key out of `config.php`)
- `sensitive: true` -- redact the value in logs

### The upstream catalogue

`files/misp-config/settings-upstream.yaml` is generated, never edited: every setting the
running MISP defines that `settings.yaml` does not name, with MISP's own default, description,
type and level, all `track_only`. The source is `cake Admin getSetting all` inside the web
container, which carries each setting's description. The image never applies those values; the file
documents them and makes every MISP setting overridable through its derived env var.

```bash
mise run settings-update              # build the images, install the chart on k3s, regenerate, delete the cluster
mise run settings-update -- --skip-build
mise run composer-lock                # resolve files/composer.lock for the current CORE_TAG
mise run misp-requirements           # resolve files/requirements-misp.txt for the current CORE_TAG
```

`files/composer.lock` pins MISP's PHP dependencies plus this image's extra packages. The
build installs exactly that lock and fails when it no longer matches upstream's
`composer.json`, which is the signal to run `mise run composer-lock` after a bump.

`files/requirements-misp.txt` pins the Python packages MISP's own scripts import (STIX,
TAXII, ZeroMQ, sample extraction), resolved from MISP's `requirements.txt`. The upstream
guard fails while MISP asks for a package or a version the pins do not give.

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

## Docs from the code

The docs state no fact the code holds by hand. Two mechanisms keep them in step, and the unit
tests fail while either is out of step:

| Mechanism | Covers |
| --- | --- |
| Generated regions, between `<!-- generated: <name> -->` and `<!-- end generated -->`, filled by `scripts/generate_docs.py` | The periodic tasks table, the Secrets table, the value and component tables of the chart, the upstream guard's checks, the unit test modules |
| `tests/test_docs.py` | Every env var, setting, repository path, file name and mise task a doc names exists; the component, task, upstream check, metric, exit code and OIDC tables are complete; the sizes, requests, sync waves, defaults and ports quoted in sentences match the manifests and the code |

Change the source, not the doc: a task description lives in `misp_container/task.py`
(`DESCRIPTIONS`), a Secret's readers in the manifests. Then run `mise run docs`, which also
regenerates `AGENTS.md`. Measured figures (image sizes, startup times, memory, benchmark
times) have no source in the code; they carry the version they were measured on.

## Project Structure

`AGENTS.md` holds the full map, generated from the docstrings, header comments, manifests,
tasks and CI jobs by `scripts/generate_agents_md.py`. After a change to one of them, run
`mise run agents-md`; a unit test fails while the file is stale.
