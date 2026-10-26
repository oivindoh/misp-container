# Developing

## TL;DR

- `mise run test` for the unit tests. `mise run build` builds the images, and each stack suite
  (`mise run test-integration` and the others) installs the chart on a k3s cluster in podman.
- Settings: `deploy/chart/files/settings.yaml`; the chart passes each one to MISP's own
  environment layer, so it wins over the UI (docs/configuration.md, Settings).
- New MISP release: the tracking workflow opens a draft PR with the bump, `files/composer.lock`
  and `files/requirements-misp.txt`; marking it ready runs CI, and the upstream guard tells you
  what changed.
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
release, and opens a draft PR with the three changes. GitHub starts no CI for a pull request
that a workflow opens with its own token: mark it ready for review, and CI builds, scans and
runs every suite. The upstream guard (`scripts/check_upstream.py`, run in the image by
`tests/e2e/test_upstream.py`) fails when MISP changed something the image patches or depends
on (table below). A new MISP setting needs nothing: until `settings.yaml` lists it, MISP keeps
its own default, and its `MISP_SETTING_*` name sets it.

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
| `cakelog-streams` | app/Config/bootstrap.default.php | files/misp_container/init.py LOG_BLOCK | configures a CakeLog file stream that the logging block does not drop |
| `shell-streams` | app/Lib/cakephp/lib/Cake/Console/Shell.php | files/misp_container/init.py LOG_BLOCK | checks other stream names before it adds its console streams |
| `relayed-files` | app/Model, app/Lib, app/Controller, app/Console, app/Plugin, app/Error | files/misp_container/logrelay.py FILES | writes a new file under app/tmp/logs, or stops writing one the relay follows |
| `env-settings` | app/Lib/Tools/EnvSetting.php, app/Model/SystemSetting.php | deploy/chart/templates/_settings.tpl, deploy/chart/files/settings.yaml | stops reading MISP_SETTING_* settings, or a category the repository's settings use |
| `image-paths` | app/Model/Server.php, app/View/Helper/OrgImgHelper.php | deploy/chart/templates/_helpers.tpl misp.appVolumeMounts | uploads org logos or custom images to, or reads them from, a directory the volumes do not mount |
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
3. Open a pull request to master. The upstream guard and the suites report what MISP changed.
4. After the merge, release it (see Releases).

## Releases

While MISP is a work-in-progress pin, every release is the next pre-release of the version the
pin prepares, for the git tag and the images: `v3.0.0-wip.1`, then `v3.0.0-wip.2` and on. The
chart takes the version that the next release of the kind would take, with the same suffix:
`3.0.0-wip.1` before the first release, `4.1.0-wip.1` after chart `4.0.0`. The kind defaults to
`normal` then.

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
4. Refuses a chart version that GHCR has already: `helm push` would replace it.
5. Sets `version` and `appVersion` in `deploy/chart/Chart.yaml`.
6. Shows the plan and the diff. On a yes it commits and tags; on a no it restores the files.

Publish the release. `--atomic` lands the branch and the tag together:

```bash
git push --atomic origin master <tag>
```

The push starts two runs of the release commit. The `gate` job stops the master run, and the
tag run tests the commit and releases it.

CI runs the tests and the scans, then pushes the images for amd64 and arm64 and the chart,
and creates a GitHub Release that names the MISP commit. The `publish` job refuses a tag whose image tag differs
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

## Settings

`deploy/chart/files/settings.yaml` holds the settings this repository defines; the chart
resolves them with the env value into env vars (`deploy/chart/templates/_settings.tpl`).
[docs/configuration.md](docs/configuration.md#settings) describes what MISP does with them.

### Adding a setting

Put it in a group with its value. The env var that replaces the value is derived from its name
(`MISP.my_setting` -> `MISP_MY_SETTING`); nothing else to declare.

```yaml
settings:
  optional:
    MISP.my_setting:
      value: some-default
```

`blank_protection: true` leaves an empty value out, so MISP keeps its own default until env
sets one. `derived` and `aliases` at the end of the file map one env var onto others.

### Groups

| Group | Reaches MISP through |
|-------|----------------------|
| `minimum_config`, `db_enable` | `MISP_SETTING_*` and `config.php`, which every pod renders from `MISP_CONFIG_*` on start |
| `s3` | the same, while `PLUGIN_S3_BUCKET_NAME` is set |
| `oidc`, `ldap` | `config.php` only, while the plugin's switch is on: MISP takes no auth plugin key from its environment layer |
| every other group | `MISP_SETTING_*` |

MISP reads `config.php` before its environment layer, and its bootstrap reads `MISP.baseurl`,
`debug` and the auth plugin keys from it. MISP never reads `SystemSetting::BLOCKED_SETTINGS`
(salt, encryption key, password policy, `python_bin`, `ca_path`, `tmpdir`, `attachments_dir`,
`system_setting_db` and a few more) from the database. Those settings stay in
`minimum_config`.

```bash
mise run composer-lock                # resolve files/composer.lock for the current CORE_TAG
mise run misp-requirements           # resolve files/requirements-misp.txt for the current CORE_TAG
```

`files/composer.lock` pins MISP's PHP dependencies plus this image's extra packages. The
build installs exactly that lock and fails when it no longer matches upstream's
`composer.json`, which is the signal to run `mise run composer-lock` after a bump.

`files/requirements-misp.txt` pins the Python packages MISP's own scripts import (STIX,
TAXII, ZeroMQ, sample extraction), resolved from MISP's `requirements.txt` at the pinned
commit; a unit test fails while its header names another commit.

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
