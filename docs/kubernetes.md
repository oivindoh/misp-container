# Kubernetes

## TL;DR

- `deploy/chart` is a Helm chart. By default it renders MISP itself; each component (database,
  cache, ingress or HTTPRoute, network policies, CronJobs) switches on with
  `<component>.enabled`.
- Set real values in `secrets.db`, `secrets.app` and `secrets.admin`, or set
  `secrets.create: false` and supply the Secrets. The placeholders make the configure Job
  refuse to run.
- Attachments need a ReadWriteMany claim or S3. The task runner in CronJobs does all
  periodic work.

## Install

Each release publishes the chart to GHCR:

```bash
helm install misp oci://ghcr.io/oivindoh/charts/misp --version 1.0.0 \
    --namespace misp --create-namespace -f values.yaml
```

The chart has its own SemVer version. Its `appVersion` is the image tag of the release: the
MISP version, with `-rN` for a hotfix. `helm list` shows it as the app version.

| Release | Chart version | `appVersion` |
|---------|---------------|--------------|
| A normal release, such as a new MISP version | the minor goes up: `1.1.0` | `2.5.49` |
| A hotfix | the patch goes up: `1.1.1` | `2.5.49-r1` |
| A change that breaks existing values or the upgrade path | the major goes up: `2.0.0` | the release's image tag |

So a version range such as `1.*` in an Argo CD `targetRevision` or a Flux HelmRelease
follows every MISP release and hotfix, and stops before a change to the values. From a
checkout, install `deploy/chart` instead.

```yaml
# values.yaml
env:
  MISP_BASEURL: https://misp.example.com
  ADMIN_EMAIL: admin@example.com
  MISP_UUID: 06732f0d-87e3-4ede-8767-4e0e9879414c
secrets:
  db: {DB_PASSWORD: ..., MYSQL_ROOT_PASSWORD: ...}
  app: {MISP_REDIS_PASSWORD: ..., GNUPG_PASSWORD: ..., SECURITY_SALT: ...}
  admin: {ADMIN_PASSWORD: ..., ADMIN_KEY: ...}
mariadb: {enabled: true}
redis: {enabled: true}
ingress:
  enabled: true
  host: misp.example.com
cronjobs: {enabled: true}
```

The chart names its objects as `web`, `worker`, `misp-env` and so on, so a namespace holds
one release. The comments in `deploy/chart/values.yaml` describe every value; the tables
below come from them.

<!-- generated: values -->
| Value | Is |
|---|---|
| `image` | The three images, all with one tag. An empty tag is the chart's appVersion. |
| `imagePullSecrets` | Secrets with the registry credentials for the images, by name. |
| `nodeSelector` | Node selection for every pod of the release. |
| `tolerations` | Tolerations for every pod of the release. |
| `affinity` | Affinity for every pod of the release. |
| `env` | The misp-env ConfigMap: these entries on top of files/base.env. Every MISP pod reads it. |
| `secrets` | The misp-db, misp-app and misp-admin Secrets: these entries on top of files/secrets-*.env, whose placeholders the configure Job refuses. With create: false the chart renders none of them: supply Secrets with the same names and keys (SOPS, External Secrets, Sealed Secrets). |
| `configure` | The configure Job: schema, settings, admin user, GPG and auth, on every install and upgrade. |
| `web` | MISP's web tier: PHP-FPM and the caddy sidecar in each pod. Replicas scale freely. |
| `worker` | The background workers. Replicas scale freely; the web pods reach supervisord on TCP 9001. |
| `metrics` | The Prometheus exporter (docs/metrics.md). |
| `modules` | misp-modules: the enrichment, import, export and action modules. |
| `orgSync` | The org-sync Job applies orgs, the content of orgs.yaml (deploy/orgs.yaml.example), through the API after each install and upgrade. It exits at once when ADMIN_KEY is empty. |
| `attachments` | Attachments, org logos and custom images on the claim attachments. Web and worker pods on different nodes write there, so the storage class must offer ReadWriteMany. For attachments in S3, set PLUGIN_S3_BUCKET_NAME in env and claim: false; org logos and custom images then stay in each pod. |
<!-- end generated -->

## Components

Each component is off by default:

<!-- generated: components -->
| Component | Adds |
|---|---|
| `mariadb` | Single-node MariaDB on a ReadWriteOnce claim. Leave it off for an external database (DB_HOST in env). |
| `postgres` | Single-node PostgreSQL on a ReadWriteOnce claim, as the alternative to mariadb. It sets DB_ENGINE, DB_HOST and DB_PORT in misp-env. Leave it off for an external PostgreSQL (StackGres and the like). |
| `redis` | Single Redis without persistence: sessions and queued jobs do not survive a restart. Leave it off for an external Redis (MISP_REDIS_HOST in env). |
| `ingress` | An Ingress to the web Service. The default annotation makes the haproxy ingress replace X-Forwarded-For with the address it sees (docs/kubernetes.md, Client addresses). |
| `httpRoute` | An HTTPRoute for a Gateway API implementation, in place of the ingress. The Gateway must allow routes from this namespace; set TRUSTED_PROXY_CIDR in env to its proxy pods. |
| `ciliumNetworkPolicy` | CiliumNetworkPolicies for every pod: the paths MISP needs, DNS, HTTPS out and SMTP out. ingressNamespace holds the ingress or Gateway proxies, monitoringNamespace Prometheus. |
| `pdb` | PodDisruptionBudgets for web and worker (maxUnavailable: 1). |
| `cronjobs` | The periodic tasks as CronJobs: tasks maps a task to its schedule, workflows a workflow ID to its schedule. API tasks need ADMIN_KEY in secrets.admin and dispatch nothing while maxQueued jobs or more wait. |
| `userValidity` | A daily check of every account against the OIDC or LDAP provider, through MISP's console. Needs the oidc or ldap group. check-user-validity reports; block-invalid-users disables the accounts the provider no longer backs. |
| `housekeeping` | Nightly deletes of old rows in jobs, logs and audit_logs (HOUSEKEEPING_<TABLE>_DAYS in env, defaults 2, 30 and 90). |
| `migrate` | A copy of an existing MySQL or MariaDB MISP into the database, before the configure Job runs (docs/migration.md). secret: entries on top of files/secrets-migrate.env for the misp-migrate Secret. volumes and volumeMounts: the source's attachments directory. |
<!-- end generated -->

## Client addresses

MISP logs the first address in `X-Forwarded-For` as the client of each request. The caddy
sidecar sends it one address: the rightmost `X-Forwarded-For` address outside the trusted
proxies, `TRUSTED_PROXY_CIDR`, or the address of the peer when the peer is no trusted proxy.
When every address in the header is trusted, it is the peer's. So an address a client wrote
into the header never reaches the audit log, whether the proxy in front replaces the header
(the `ingress` component's default annotation does) or appends to it (as a Gateway does).

| Setup | `TRUSTED_PROXY_CIDR` | MISP logs |
|-------|----------------------|-----------|
| Kubernetes, unset | every private range | a client with a public address; for a client with a private address, the ingress or Gateway pod |
| Kubernetes, set in `env` to the ingress or Gateway proxy pods | those pods | every client |
| Compose | `127.0.0.1/32` on the caddy service: clients reach caddy directly | every client |

With `ciliumNetworkPolicy`, only the ingress namespace reaches the web pods. For a Gateway,
set `ciliumNetworkPolicy.ingressNamespace` to the namespace of the Gateway's proxies.

## Secrets

The chart makes its Secrets from `.env` files that Compose reads too, with the `secrets`
value on top; the `migrate` component adds `misp-migrate`, with `migrate.secret` on top. A
workload gets a whole Secret, or only the keys the table names:

<!-- generated: secrets -->
| Secret | File | Keys | Read by |
|---|---|---|---|
| `misp-db` | `deploy/chart/files/secrets-db.env` | `DB_USER`, `DB_PASSWORD`, `MYSQL_ROOT_PASSWORD` | configure, cronjobs, housekeeping, mariadb (`DB_PASSWORD`, `DB_USER`, `MYSQL_ROOT_PASSWORD`), metrics (`DB_PASSWORD`, `DB_USER`), migrate, org-sync, postgres (`DB_PASSWORD`, `DB_USER`), userValidity, web, worker |
| `misp-app` | `deploy/chart/files/secrets-app.env` | `MISP_REDIS_PASSWORD`, `GNUPG_PASSWORD`, `SECURITY_ENCRYPTION_KEY`, `SECURITY_SALT` | configure, cronjobs, metrics (`MISP_REDIS_PASSWORD`), migrate, redis (`MISP_REDIS_PASSWORD`), userValidity, web, worker |
| `misp-admin` | `deploy/chart/files/secrets-admin.env` | `ADMIN_PASSWORD`, `ADMIN_KEY` | configure, cronjobs (`ADMIN_KEY`), org-sync |
| `misp-migrate` | `deploy/chart/files/secrets-migrate.env` | `MIGRATE_SOURCE_HOST`, `MIGRATE_SOURCE_USER`, `MIGRATE_SOURCE_PASSWORD`, `MIGRATE_FORCE`, `MIGRATE_REPLACE_COPY`, `MIGRATE_SOURCE_FILES`, `MIGRATE_SOURCE_S3_BUCKET`, `MIGRATE_SOURCE_S3_ENDPOINT`, `MIGRATE_SOURCE_S3_REGION`, `MIGRATE_SOURCE_S3_ACCESS_KEY`, `MIGRATE_SOURCE_S3_SECRET_KEY` | migrate |
<!-- end generated -->

With `ADMIN_KEY` empty, MISP generates a key, org-sync exits without changes, and the API
task CronJobs fail with a clear message.

The files hold placeholders that the configure Job refuses. Set the real values in the
`secrets` value, or set `secrets.create: false` and supply Secrets with the same names and
keys, for example through SOPS, External Secrets or Sealed Secrets. The pods roll when a
Secret the chart makes changes; after a change to a Secret you supply, restart them.

These Secrets are optional and copied into every pod at start:

| Secret | Content | Command |
|--------|---------|---------|
| `misp-gnupg` | `private.asc`, the instance GPG key (armoured secret key export). `GNUPG_PASSWORD` is its passphrase; set `GNUPG_SIGN=true` once it exists | `kubectl -n misp create secret generic misp-gnupg --from-file=private.asc` |
| `misp-certs` | `<server id>.pem` files for sync servers that need a pinned certificate | `kubectl -n misp create secret generic misp-certs --from-file=3.pem` |

```bash
gpg --batch --passphrase "$GNUPG_PASSWORD" --quick-generate-key "MISP Admin <misp@example.com>" rsa3072 sign never
gpg --armor --export-secret-keys misp@example.com > private.asc
```

## Storage

| Resource | Default | Notes |
|----------|---------|-------|
| MariaDB or PostgreSQL | StatefulSet, 20Gi ReadWriteOnce claim (`mariadb` or `postgres` component) | `mariadb.size` and `mariadb.storageClass`, or the same under `postgres` |
| Attachments, org logos, custom images | claim `attachments`, 20Gi ReadWriteMany | Web and worker pods share it, and `helm uninstall` leaves it. For S3, set `PLUGIN_S3_BUCKET_NAME` in `env` and `attachments.claim: false` |
| Redis | No persistence (`redis` component) | Sessions and queued jobs are lost when Redis restarts |
| Everything else | `emptyDir` per pod | `app/Config`, `app/tmp`, `.gnupg`, `app/files/{scripts/tmp,certs,terms}` |

The web and worker entrypoints refuse to start when the attachments directory is not
writable and S3 is not configured. A sync server certificate uploaded through the UI lands
on one replica only; use the `misp-certs` Secret.

## Network policies

The `ciliumNetworkPolicy` component allows only the paths in the diagram in
[architecture.md](architecture.md) plus DNS, HTTPS to the outside for web, worker, modules,
metrics and the console tasks, and SMTP (ports 25, 465 and 587, in or outside the cluster)
for web, worker and the console tasks. A mail relay on another port needs a change to the
template. `ciliumNetworkPolicy.ingressNamespace` (default `haproxy-controller`) and
`ciliumNetworkPolicy.monitoringNamespace` (default `monitoring`) name the namespaces of the
ingress and of Prometheus. The header of `deploy/chart/templates/ciliumnetworkpolicy.yaml`
lists every flow.

## Periodic tasks

The task runner in the main image does all periodic work. MISP's own scheduler
(`scheduler_worker`) never runs, so a task enabled under MISP's Scheduled tasks page runs
nowhere; `misp_scheduled_tasks_enabled` counts them (see [metrics.md](metrics.md)).
Manual actions in the UI and the API, such as a pull, a push or fetching one event from a
remote server, do not use a scheduler: the workers run them.

<!-- generated: periodic-tasks -->
| Task | Does | Kind | CronJob schedule (component) |
|---|---|---|---|
| `cache-feeds` | Cache every feed | API | `20 2 * * *` (`cronjobs`) |
| `fetch-feeds` | Fetch every enabled feed | API | `30 2 * * *` (`cronjobs`) |
| `cache-servers` | Cache the events of every server | API | `40 2 * * *` (`cronjobs`) |
| `update-galaxies` | Update MISP's bundled galaxies | API | `0 3 * * *` (`cronjobs`) |
| `update-taxonomies` | Update MISP's bundled taxonomies | API | `10 3 * * *` (`cronjobs`) |
| `update-warninglists` | Update MISP's bundled warninglists | API | `20 3 * * *` (`cronjobs`) |
| `update-noticelists` | Update MISP's bundled noticelists | API | `30 3 * * *` (`cronjobs`) |
| `update-object-templates` | Update MISP's bundled object templates | API | `40 3 * * *` (`cronjobs`) |
| `pull-servers` | Pull from every server with pull enabled | API | `*/5 * * * *` (`cronjobs`) |
| `push-servers` | Push to every server with push enabled | API | `*/15 * * * *` (`cronjobs`) |
| `push-taxii` | Push to every enabled TAXII server | API | `7 * * * *` (`cronjobs`) |
| `sharing-group-blueprints` | Apply the sharing group blueprints | API | `37 * * * *` (`cronjobs`) |
| `workflow` | Run one ad-hoc workflow, by its ID | API | none |
| `periodic-summary` | Send the daily, weekly (Mondays) and monthly (the first) summaries users subscribed to | console | `0 6 * * *` (`cronjobs`) |
| `check-user-validity` | Report every account as valid or invalid at the OIDC or LDAP provider | console | `30 5 * * *` (`userValidity`) |
| `block-invalid-users` | Disable the accounts the OIDC or LDAP provider no longer backs | console | none |
<!-- end generated -->

API tasks call the MISP API with `ADMIN_KEY`, which must be set. Before dispatching, a run
reads the queue depth (`misp_jobs_queued`, which the metrics exporter reads from MISP's job
queues in Redis) and dispatches nothing while `TASK_MAX_QUEUED` jobs (default 200) or more
are waiting or running, so a slow worker pool does not pile up work. `cronjobs.maxQueued` sets
it. An unreachable exporter or Redis does not block dispatch. A partner that refuses a pull or
push is logged and shows in the metrics; a run exits 1 only when no call succeeded.

Console tasks have no API. The pod renders `app/Config` and runs MISP's console, with the
configure Job's volumes and the `misp-db` and `misp-app` Secrets. The periodic summary
needs a mail relay (`SMTP_FQDN`, `SMTP_PORT`).

`cronjobs.tasks` maps each task to its schedule: change a schedule, or set it to `null` to
drop the CronJob. `cronjobs.workflows` maps a workflow ID to a schedule, one CronJob
`workflow-<id>` each. Every task CronJob pod carries `app.kubernetes.io/component:
misp-task` (API) or `misp-console-task` (console); the network policies select on that
label. To disable users instead of reporting them, set `userValidity.task` to
`block-invalid-users`.

On demand:

```bash
kubectl -n misp create job --from=cronjob/pull-servers pull-now
podman compose run --rm --no-deps sync python3 -m misp_container.task pull-servers   # Compose, API task
podman compose exec worker python3 -m misp_container.task periodic-summary          # Compose, console task
```
