# AGENTS.md

A map of this repository for agents. The tables between `<!-- generated -->` markers come
from the tree: `mise run docs` fills them (`scripts/generate_docs.py`), and a unit test fails
while one is stale. To change a row, change the docstring, comment or code it comes from.

## Read first

<!-- generated: read-first -->
| File | Title |
|---|---|
| `README.md` | MISP Container |
| `DEVELOPING.md` | Developing |
| `tests/README.md` | Testing |
| `docs/architecture.md` | Architecture |
| `docs/configuration.md` | Configuration |
| `docs/kubernetes.md` | Kubernetes |
| `docs/metrics.md` | Prometheus Metrics |
| `docs/migration.md` | Migrating an existing MISP instance |
| `docs/operations.md` | Operations |
| `docs/org-sync.md` | Declarative org sync |
<!-- end generated -->

## Image

The `Dockerfile` builds the images from these stages:

<!-- generated: image-stages -->
| Stage | What it holds |
|---|---|
| php-base | Common runtime packages |
| misp-source | Clone MISP, set permissions |
| composer | PHP dependencies, pinned by files/composer.lock |
| php-build | Native PHP extensions, built from pinned sources |
| uv | Python package installer (pinned) |
| python | Python packages of misp_container and of MISP's scripts |
| caddy-bin | The caddy binary for the web pod's caddy container |
| final | Runtime image (non-root) |
| modules | MISP enrichment/import/export/action modules |
<!-- end generated -->

Each role of the main image is a module of the package, run under the image's tini entrypoint.
The chart's workloads start them so:

<!-- generated: roles -->
| Command | Run by, every component on | Does |
|---|---|---|
| `python3 -m misp_container.configure` | Job `configure-<revision>` | One-shot MISP configuration: schema, updates, admin user and organisation. |
| `python3 -m misp_container.housekeeping` | CronJob `housekeeping-audit-logs`, CronJob `housekeeping-jobs`, CronJob `housekeeping-logs` | Nightly deletes in MISP's jobs, logs and audit_logs tables. |
| `python3 -m misp_container.metrics` | Deployment `metrics` | MISP Prometheus metrics exporter. |
| `python3 -m misp_container.migrate` | Job `configure-<revision>` | Copy an existing MISP database (MySQL or MariaDB) into this deployment's PostgreSQL. |
| `python3 -m misp_container.sync` | Job `org-sync-<revision>` | Declarative org/team sync engine. |
| `python3 -m misp_container.task` | CronJob `cache-feeds`, CronJob `cache-servers`, CronJob `fetch-feeds`, CronJob `periodic-summary`, CronJob `pull-servers`, CronJob `push-servers`, CronJob `push-taxii`, CronJob `update-galaxies`, CronJob `update-noticelists`, CronJob `update-object-templates`, CronJob `update-taxonomies`, CronJob `update-warninglists` | Periodic MISP tasks. |
| `python3 -m misp_container.web` | Deployment `web` | MISP's web role: prepares app/Config, waits for the configure Job, then execs PHP-FPM. |
| `python3 -m misp_container.worker` | Deployment `worker` | Background workers: MISP's queue workers and the log relay, each restarted when it exits. |
<!-- end generated -->

## Python library: `files/misp_container/`

The roles, the task runner and the Jobs share this package; it is `/opt/misp_container` in the
image.

<!-- generated: library -->
| Module | Does |
|---|---|
| `__init__.py` | MISP container library: the roles of the main image, the task runner and the Jobs. |
| `admin.py` | Admin user, organisation and API key setup. |
| `api.py` | Thin MISP REST API client using only urllib (stdlib). |
| `cake.py` | Thin wrapper around the CakePHP cake CLI. |
| `config.py` | config.php from the MISP_CONFIG_* env vars the chart renders. |
| `configure.py` | One-shot MISP configuration: schema, updates, admin user and organisation. |
| `db.py` | Database access for the entrypoints, on PostgreSQL. |
| `env.py` | Environment variable handling. |
| `exporter.py` | The metrics exporter's HTTP server: /metrics, and /healthz and /ready for the probes. |
| `housekeeping.py` | Nightly deletes in MISP's jobs, logs and audit_logs tables. |
| `init.py` | Per-pod preparation: tmp directories, app/Config rendering, GPG key import. |
| `log.py` | Logging setup for MISP container entrypoints. |
| `logrelay.py` | Relay the log files MISP writes directly to stdout, and keep them small. |
| `metrics.py` | MISP Prometheus metrics exporter. |
| `migrate.py` | Copy an existing MISP database (MySQL or MariaDB) into this deployment's PostgreSQL. |
| `s3.py` | A minimal S3 client for the migrate Job: list, get and put objects. |
| `sync.py` | Declarative org/team sync engine. |
| `task.py` | Periodic MISP tasks. |
| `web.py` | MISP's web role: prepares app/Config, waits for the configure Job, then execs PHP-FPM. |
| `worker.py` | Background workers: MISP's queue workers and the log relay, each restarted when it exits. |
<!-- end generated -->

MISP's own scheduler never runs. The task runner (`python3 -m misp_container.task <task>`) does
each piece of periodic work from a CronJob: docs/kubernetes.md (Periodic tasks) lists the tasks
and their schedules.

## Kubernetes: the Helm chart in `deploy/chart/`

The values and the components are in docs/kubernetes.md (Values, Components). The templates:

<!-- generated: chart-templates -->
| Template | Renders, every component on | Note |
|---|---|---|
| `configmap-env.yaml` | ConfigMap `misp-env` | misp-env: the env value (misp.envValues). Every MISP pod reads it; its checksum rolls the Deployments. With databaseCA, DB_SSLROOTCERT names the certificate's mount. |
| `configmap-orgs.yaml` | ConfigMap `misp-orgs` | Declarative org config for the org-sync Job (orgSync.orgs; format: deploy/orgs.yaml.example). |
| `configmap-settings.yaml` | ConfigMap `misp-settings` | misp-settings: the settings the repository brings (files/settings.yaml, with the env value on top) as MISP's own env layer and as config.php entries (_settings.tpl). The pods that run MISP read it; a setting a Secret holds is an env entry of their container instead. Its checksum rolls the pods. |
| `cronjobs-housekeeping.yaml` | CronJob `housekeeping-audit-logs`, CronJob `housekeeping-jobs`, CronJob `housekeeping-logs` | The housekeeping component: nightly batched deletes of old rows in jobs, logs and audit_logs (python3 -m misp_container.housekeeping). Autovacuum reclaims the space. Retention is a policy choice: HOUSEKEEPING_<TABLE>_DAYS in env. |
| `cronjobs.yaml` | CronJob `cache-feeds`, CronJob `cache-servers`, CronJob `fetch-feeds`, CronJob `periodic-summary`, CronJob `pull-servers`, CronJob `push-servers`, CronJob `push-taxii`, CronJob `update-galaxies`, CronJob `update-noticelists`, CronJob `update-object-templates`, CronJob `update-taxonomies`, CronJob `update-warninglists` | The cronjobs component: the periodic tasks, run by the task runner in the main image (python3 -m misp_container.task <task>), one CronJob per entry of cronjobs.tasks with a schedule and one per workflow ID in cronjobs.workflows. Each runs MISP's console in its pod, so the periodic work never waits in the workers' queues. |
| `deployment-metrics.yaml` | Deployment `metrics` | Prometheus exporter on port 9191: MISP's database, the job queues in Redis and the sync partners (docs/metrics.md). It reads MISP's data and never changes it. |
| `deployment-modules.yaml` | Deployment `modules` | misp-modules: the enrichment, import, export and action modules MISP calls over HTTP on port 6666. A distroless image without a shell, as UID 65532. |
| `deployment-web.yaml` | Deployment `web` | MISP's web tier: PHP-FPM (127.0.0.1:9002) and a caddy sidecar that serves the static files and passes the rest to PHP-FPM over FastCGI (port 8080). Replicas scale freely: each pod renders its own app/Config and waits for the configure Job. |
| `deployment-worker.yaml` | Deployment `worker` | Scalable job workers -- safe to run multiple replicas. MISP's own scheduler never runs: periodic tasks are Kubernetes CronJobs (the cronjobs component). |
| `httproute.yaml` | HTTPRoute `misp` | The httpRoute component: an HTTPRoute for a Gateway API implementation, in place of the ingress. caddy takes the client address from X-Forwarded-For, right to left, past TRUSTED_PROXY_CIDR: set that in env to the Gateway's proxy pods. |
| `ingress.yaml` | Ingress `misp` | The ingress component: an Ingress to the web Service. The default annotation makes the haproxy ingress replace X-Forwarded-For with the address it sees, so only that address reaches caddy (docs/kubernetes.md, Client addresses). Behind another load balancer, that is the balancer's address unless it speaks the PROXY protocol to HAProxy. |
| `job-configure.yaml` | Job `configure-<revision>` | One-shot configuration: schema, MISP's updates, admin user and organisation. Web and worker pods wait until this Job has recorded their image version and configuration checksum, so the order of the rollout does not depend on the tool that applies the chart. Each release revision has its own Job, because a Job's pod template cannot change; a run on a configured instance takes seconds. With the migrate component, the copy runs first, in an init container. |
| `job-org-sync.yaml` | Job `org-sync-<revision>` | Declarative org sync: applies the misp-orgs ConfigMap (orgs.yaml) through the MISP API once the configure run of its revision finished and web serves. Exits 0 at once when ADMIN_KEY is empty or orgs.yaml has nothing to apply. Each release revision has its own Job; Argo CD: a Sync hook in wave 3, recreated on every sync. |
| `networkpolicy.yaml` | NetworkPolicy `misp`, NetworkPolicy `misp-web`, NetworkPolicy `misp-metrics`, NetworkPolicy `misp-egress`, NetworkPolicy `misp-modules`, NetworkPolicy `misp-stores` | The networkPolicy component: NetworkPolicies for the pods of the release, for any CNI that enforces the Kubernetes NetworkPolicy API, Cilium included. Every pod template carries app.kubernetes.io/instance (the release) and app.kubernetes.io/part-of: misp, and every rule selects on them, so the pods of another release or an operator in the namespace keep their own rules. |
| `pdb.yaml` | PodDisruptionBudget `web`, PodDisruptionBudget `worker` | One voluntary disruption at a time for web and worker. maxUnavailable, not minAvailable, so a single replica never blocks a node drain. |
| `postgres.yaml` | Service `postgres`, StatefulSet `postgres` | The postgres component: single-node PostgreSQL on a ReadWriteOnce claim, the database misp owned by DB_USER, UTF8, behind the Service postgres that DB_HOST names by default. |
| `pvc-attachments.yaml` | PersistentVolumeClaim `attachments` | Attachments and malware samples, org logos and custom images. Web and worker pods on different nodes write here, so the storage class must support ReadWriteMany. With attachments.claim: false (attachments in S3) the chart renders no claim. |
| `redis.yaml` | Service `redis`, Deployment `redis` | The redis component: a single Redis without persistence, so sessions and queued jobs do not survive a restart. The password is MISP_REDIS_PASSWORD. |
| `secrets.yaml` | Secret `misp-db`, Secret `misp-app`, Secret `misp-admin`, Secret `misp-migrate` | The Secrets follow their consumers: web and worker pods get misp-db and misp-app, never misp-admin; metrics gets two keys of misp-db, and of misp-app the Redis and proxy passwords. Each holds its map of the secrets value; with secrets.create: false the release supplies them. |
| `service-metrics.yaml` | Service `metrics` | The exporter on port 9191, for Prometheus to scrape. |
| `service-modules.yaml` | Service `modules` | misp-modules on port 6666, for MISP's enrichment, import, export and action calls. |
| `service-web.yaml` | Service `web` | MISP over HTTP: caddy in the web pods, port 8080. The ingress or HTTPRoute routes here. |
| `tests/test-misp.yaml` | Pod `misp-test` | helm test: checks a release from inside the cluster. It loads the login page through the web Service and, with ADMIN_KEY in misp-admin, calls the API as the admin and reads MISP.live. |
<!-- end generated -->

## Settings: `deploy/chart/files/settings.yaml`

MISP's settings in groups, as the repository defines them: each wins over a change in MISP's
UI. A setting's env var is its name in upper case with dots as underscores
(docs/configuration.md, Settings).

<!-- generated: settings-groups -->
| Group | Holds |
|---|---|
| `minimum_config` | also rendered into config.php, by every pod. MISP never reads SystemSetting::BLOCKED_SETTINGS from the database (salt, encryption key, password policy, python_bin, ca_path, tmpdir, attachments_dir, self_update, online_version_check, system_setting_db, GnuPG.binary, disable_local_feed_access), so those live here. |
| `db_enable` | toggle system_setting_db |
| `initialisation` | core instance settings |
| `critical` | security hardening |
| `optional` | nice-to-have defaults |
| `gpg` | GnuPG settings |
| `s3` | S3 attachment storage (only while PLUGIN_S3_BUCKET_NAME is set, which also sets MISP.attachments_dir to s3://) |
| `proxy` | HTTP proxy settings (an empty Proxy.host means no proxy) |
| `custom_auth` | header authentication from a reverse proxy, no directory lookup (CUSTOM_AUTH_ENABLE=true turns it on; the CUSTOM_AUTH_* names are aliases, below) |
| `kafka` | Kafka pub/sub integration (PLUGIN_KAFKA_ENABLE=true turns it on) |
| `oidc` | OidcAuth plugin, rendered into config.php when OIDC_ENABLE=true. The documented OIDC_* names map onto these (the aliases below). role_mapper and scopes take JSON in env; scopes also takes a comma-separated list. |
| `ldap` | LdapAuth plugin, rendered into config.php when LDAPAUTH_ENABLE=true |
<!-- end generated -->

## MISP itself

`scripts/check_upstream.py` fails when MISP no longer matches what this image patches or
depends on; DEVELOPING.md (What the image depends on in MISP) lists its checks. The image
build applies the patches of `patches/` (DEVELOPING.md, Patches to MISP):

<!-- generated: patches -->
| Patch | MISP file | Change | Upstream |
|---|---|---|---|
| `01-event-index-stats-quotes-the-alias.patch` | app/Model/Behavior/EventIndexStatsBehavior.php | Quote the alias of the events index stats | not filed yet |
| `02-a-job-keeps-the-workers-environment.patch` | app/Lib/Tools/BackgroundJobs/BackgroundJob.php | A background job keeps the worker's environment | not filed yet |
| `03-a-worker-finishes-its-job-on-sigterm.patch` | app/Console/Command/StartWorkerShell.php, app/Lib/Tools/BackgroundJobs/BackgroundJob.php | A worker finishes its running job on SIGTERM | not filed yet |
<!-- end generated -->

## Scripts: `scripts/`

<!-- generated: scripts -->
| Script | Does |
|---|---|
| `apply_patches.py` | Apply this repository's patches to a MISP tree, and fail when one no longer fits. |
| `build.py` | Build the two images of this tree under one tag: misp and modules. |
| `chart.py` | Render the Helm chart in deploy/chart for the doc generators and the tests. |
| `check_upstream.py` | Fail when MISP no longer matches what this image patches or depends on. |
| `generate_docs.py` | Fill the generated regions of the docs and AGENTS.md from the code. |
| `release.py` | Prepare a release. |
| `update-composer-lock.sh` | Resolve MISP's composer dependencies for the current CORE_TAG plus this image's extra packages, and write the result to files/composer.lock. The image build installs exactly that lock and fails when it is out of date with upstream's composer.json (a new MISP release), so run this on every bump. |
| `update_misp_requirements.py` | Resolve the packages MISP's own scripts import into files/requirements-misp.txt. |
<!-- end generated -->

## Tests: `tests/`

The unit tests are `tests/test_*.py`, one module per part; tests/README.md lists them. The
stack suites, their harness and the smoke test, in pytest (`tests/e2e/`):

<!-- generated: stack-tests -->
| Module | Does |
|---|---|
| `cluster.py` | A single-node k3s cluster in a privileged container, for the stack suites. |
| `conftest.py` | Options and shared fixtures for the stack suites (tests/e2e). |
| `garage.py` | The garage S3 store of the stack suites: layout, keys and buckets through its admin API. |
| `keycloak.py` | The browser side of a Keycloak login to MISP, run in the toolbox of a release under test. |
| `manifests.py` | The test services the stack suites run beside the chart, as documents for Cluster.apply(). |
| `stack.py` | A release of the chart under test: helm, pod exec, SQL, HTTP and waits. |
| `test_integration.py` | Integration suite: one release of the chart on k3s, checked feature by feature. |
| `test_lifecycle.py` | Lifecycle suite: the chart from its install to a rollback, on k3s. |
| `test_migration.py` | Migration suite: loads a MISP 2.5 database into MariaDB, MISP's own INSTALL/MYSQL.sql of 2.5 with the rows of tests/migrate-source.sql, migrates it into PostgreSQL with the chart's migrate component, which converts it to MISP 3.0 on the way, and checks the copy through the API and the database. The first copy, in the configure Job, takes the attachments from a claim onto the attachments claim; on it, an org sync renames the migrated organisation to its Keycloak organisation's alias, and a migrated account logs in through Keycloak. A forced second copy takes the attachments into an S3 bucket in garage; a last run copies that bucket into another. Every run after the first runs migrate in the toolbox, with the env of the run. |
| `test_smoke.py` | Smoke test of a live MISP through its API: reachable, authenticated, configured, working. |
| `test_sync.py` | Hub-spoke sync suite: two releases of the chart and a MISP 2.5 partner on one k3s cluster. |
| `test_upstream.py` | Upstream guard: scripts/check_upstream.py in the MISP image, one test per check. |
<!-- end generated -->

The values and manifests of the stack suites:

<!-- generated: stack-manifests -->
| File | Is |
|---|---|
| `tests/k3s/external-redis.yaml` | A Redis outside the chart for the integration suite. Its password is in the Secret external-redis (password), which the suite makes. |
| `tests/k3s/external.yaml` | Values of the integration suite: PostgreSQL and Redis outside the chart, as a database operator runs them (tests/k3s/postgres-tls.yaml, tests/k3s/external-redis.yaml). The connection comes from their Secrets, and every pod checks the server's certificate. |
| `tests/k3s/garage.yaml` | garage: S3 on 3900 and its admin API on 3903 (tests/e2e/garage.py), single-node and ephemeral, for the attachments in S3 |
| `tests/k3s/keycloak.yaml` | Keycloak with the realm of tests/keycloak-realm.json, which the suite puts in the ConfigMap keycloak-realm. MISP and the toolbox reach it under one name, so the issuer matches |
| `tests/k3s/mariadb.yaml` | A MariaDB with one database, ephemeral: the migration suite's source, and the database of the hub-spoke suite's MISP 2.5. The suite puts MARIADB_DATABASE, MARIADB_USER and MARIADB_PASSWORD in the ConfigMap mariadb-env. MariaDB stays on its LTS line, 11.8 (.github/dependabot.yml) |
| `tests/k3s/misp-25.yaml` | MISP 2.5.50 from the MISP project's images (github.com/MISP/misp-docker): the hub-spoke suite's partner C, on the MariaDB of tests/k3s/mariadb.yaml and a Redis of its own. The suite puts the settings, in the names of those images, in the ConfigMap misp-25-env. A rebuild moves a tag, so the digests pin the images. No pod gets service links: misp-docker reads REDIS_PORT, which a link sets to tcp://<address> |
| `tests/k3s/oidc.yaml` | OIDC through the Keycloak of tests/k3s/keycloak.yaml (realm tests/keycloak-realm.json), on top of values.yaml for the integration and migration suites. Mixed auth keeps the password form for the other tests. The client roles, highest first, so the highest role a user holds wins. No default organisation: the user lands in its Keycloak organisation's team or nowhere. |
| `tests/k3s/postgres-tls.yaml` | A PostgreSQL outside the chart for the integration suite, run as a database operator runs it: password logins only over TLS, and the database owned by a user that is no superuser. The suite makes the Secrets pg-cluster-cert (ca.crt, tls.crt, tls.key) and pg-pguser-misp (host, port, dbname, user, password), in the shape the Percona operator gives them. |
| `tests/k3s/values.yaml` | The values every stack suite starts from (tests/e2e/stack.py), on k3s: the postgres and redis components, test secrets, a ReadWriteOnce attachments claim, because k3s's local-path storage offers no ReadWriteMany, and the images under the tag under test, which the suite sets. A suite adds its own values on top. |
<!-- end generated -->

## Commands

<!-- generated: commands -->
| Command | Does |
|---|---|
| `mise run test` | Run unit tests |
| `mise run build` | Build the misp and modules images under MISP_IMAGE_TAG, else the chart's appVersion |
| `mise run test-integration` | Run the upstream guard and the integration suite (k3s in podman) |
| `mise run test-sync` | Run the hub-spoke sync suite (2 MISP instances and a MISP 2.5 partner) |
| `mise run test-migration` | Run the migration suite (a MISP 2.5 database on MariaDB into PostgreSQL) |
| `mise run test-lifecycle` | Run the lifecycle suite: upgrade from the newest release, helm test, CronJobs, upgrade and rollback |
| `mise run docs` | Regenerate the generated regions of the docs and AGENTS.md from the code |
| `mise run test-all` | Run the unit tests, then every stack suite |
| `mise run smoketest` | Run smoke test against a live MISP instance |
| `mise run composer-lock` | Resolve MISP's PHP dependencies for the current CORE_TAG into files/composer.lock |
| `mise run misp-requirements` | Resolve the Python packages of MISP's own scripts for the current CORE_TAG into files/requirements-misp.txt |
| `mise run release` | Prepare a release: the next 3.0.0-wip.N while MISP is a work-in-progress pin, else of one kind (hotfix, normal or breaking) |
<!-- end generated -->

## CI: `.github/workflows/`

The jobs of `ci.yaml` (tests/README.md, CI):

<!-- generated: ci-jobs -->
| Job | Needs |
|---|---|
| `gate` |  |
| `build` | `gate` |
| `unit` | `gate` |
| `suite` | `gate`, `build` |
| `ci-ok` | `gate`, `build`, `unit`, `suite` |
| `publish-plan` | `gate`, `build`, `unit`, `suite` |
| `publish-arm64` | `publish-plan` |
| `publish` | `publish-plan`, `publish-arm64` |
<!-- end generated -->

<!-- generated: workflows -->
| Workflow | Runs on | Does |
|---|---|---|
| `.github/workflows/cache-cleanup.yaml` | pull_request | Deletes a pull request's Actions caches when it closes: nothing reads them again, and at the repository's 10 GB limit GitHub evicts the least recently used entries, master's layers among them. |
| `.github/workflows/ci.yaml` | push (master, tag v*), pull_request (master) | Tests, scans and releases the images and the Helm chart: unit tests, the chart renders, the stack suites on k3s and Trivy; on a tag, pushes the images for amd64 and arm64 and the chart and creates the release (tests/README.md, CI). |
| `.github/workflows/track-misp-releases.yaml` | schedule, workflow_dispatch | Opens a draft pull request for each new MISP release of the major version the Dockerfile pins: bumps CORE_TAG, clears a CORE_COMMIT pin, and resolves files/composer.lock and files/requirements-misp.txt (DEVELOPING.md, New MISP releases). While the pin is a work-in-progress branch such as 3.0_wip, the first release of that major version replaces it. |
<!-- end generated -->
