# Kubernetes

## TL;DR

- `deploy/chart` is a Helm chart. By default it renders MISP itself; each component (database,
  cache, ingress or HTTPRoute, network policies, CronJobs) switches on with
  `<component>.enabled`.
- Set real values in `secrets.db`, `secrets.app` and `secrets.admin`, or set
  `secrets.create: false` and supply the Secrets. The placeholders make the configure Job
  refuse to run.
- PostgreSQL and Redis are the `postgres` and `redis` components or external servers.
  `connectionSecrets` takes the connection from an operator's Secrets (CloudNativePG, Percona,
  StackGres).
- Attachments need a ReadWriteMany claim or S3. The task runner in CronJobs does all
  periodic work.

## Install

Each release publishes the chart to GHCR; Helm takes the newest one:

```bash
helm install misp oci://ghcr.io/oivindoh/charts/misp \
    --namespace misp --create-namespace -f values.yaml
```

The chart's version and its `appVersion`, the image tag of the release, follow the release
scheme in [DEVELOPING.md](../DEVELOPING.md#releases). A pre-release such as `3.0.0-wip.1` needs
`--version` or `--devel`. From a checkout, install `deploy/chart` instead.

Check a release from inside the cluster once the configure Job has run:

```bash
helm test misp --namespace misp --logs
```

The test pod loads the login page through the `web` Service. With `ADMIN_KEY` set, it also
checks through the API that `MISP.live` is true.

```yaml
# values.yaml
env:
  MISP_BASEURL: https://misp.example.com
  ADMIN_EMAIL: admin@example.com
  MISP_UUID: 06732f0d-87e3-4ede-8767-4e0e9879414c
secrets:
  db: {DB_PASSWORD: ...}
  app: {MISP_REDIS_PASSWORD: ..., GNUPG_PASSWORD: ..., SECURITY_SALT: ...}
  admin: {ADMIN_PASSWORD: ..., ADMIN_KEY: ...}
postgres: {enabled: true}
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
| `connectionSecrets` | The connection to PostgreSQL and Redis from Secrets the release does not make, such as the ones a database operator keeps (docs/kubernetes.md, External PostgreSQL and Redis): each Secret by name, with the env var that each of its keys fills. They replace the same entries of misp-env, misp-db and misp-app in every pod, the postgres and redis components included. |
| `databaseCA` | The CA certificate of the PostgreSQL server, from a Secret: a name and a key. Every pod that connects mounts it at DB_SSLROOTCERT, for DB_SSLMODE verify-ca or verify-full in env. |
| `configure` | The configure Job: schema, settings, admin user, GPG and auth, on every install and upgrade. |
| `web` | MISP's web tier: PHP-FPM and the caddy sidecar in each pod. Replicas scale freely. |
| `worker` | The background workers, which take their jobs from Redis. Replicas scale freely. |
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
| `postgres` | Single-node PostgreSQL on a ReadWriteOnce claim. Leave it off for an external PostgreSQL (DB_HOST in env or connectionSecrets; CloudNativePG, StackGres, Percona and the like). |
| `redis` | Single Redis without persistence: sessions and queued jobs do not survive a restart. Leave it off for an external Redis (MISP_REDIS_HOST in env or connectionSecrets). |
| `ingress` | An Ingress to the web Service. The default annotation makes the haproxy ingress replace X-Forwarded-For with the address it sees (docs/kubernetes.md, Client addresses). |
| `httpRoute` | An HTTPRoute for a Gateway API implementation, in place of the ingress. The Gateway must allow routes from this namespace; set TRUSTED_PROXY_CIDR in env to its proxy pods. |
| `networkPolicy` | NetworkPolicies for every pod: the paths MISP needs, DNS, HTTPS out and SMTP out, for any CNI that enforces the NetworkPolicy API, Cilium included. ingressNamespace holds the ingress or Gateway proxies, monitoringNamespace Prometheus; databaseEgress and redisEgress reach an external PostgreSQL and Redis. |
| `cronjobs` | The periodic tasks as CronJobs, each running MISP's console in its pod: tasks maps a task to its schedule, workflows a workflow ID to its schedule. The pod's PHP process has the workers' memory ceiling (WORKER_MEMORY_LIMIT, else PHP_MEMORY_LIMIT), which resources must hold. |
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

With `networkPolicy`, only the ingress namespace reaches the web pods. For a Gateway,
set `networkPolicy.ingressNamespace` to the namespace of the Gateway's proxies.

## Secrets

The chart makes its Secrets from the `.env` files in `deploy/chart/files`, with the `secrets`
value on top; the `migrate` component adds `misp-migrate`, with `migrate.secret` on top. A
workload gets a whole Secret, or only the keys the table names:

<!-- generated: secrets -->
| Secret | File | Keys | Read by |
|---|---|---|---|
| `misp-db` | `deploy/chart/files/secrets-db.env` | `DB_USER`, `DB_PASSWORD` | configure, cronjobs, housekeeping, metrics (`DB_PASSWORD`, `DB_USER`), migrate, org-sync, postgres (`DB_PASSWORD`, `DB_USER`), web, worker |
| `misp-app` | `deploy/chart/files/secrets-app.env` | `MISP_REDIS_PASSWORD`, `GNUPG_PASSWORD`, `SECURITY_ENCRYPTION_KEY`, `SECURITY_SALT` | configure, cronjobs, metrics (`MISP_REDIS_PASSWORD`), migrate, redis (`MISP_REDIS_PASSWORD`), web, worker |
| `misp-admin` | `deploy/chart/files/secrets-admin.env` | `ADMIN_PASSWORD`, `ADMIN_KEY` | configure, misp-test (`ADMIN_KEY`), org-sync |
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

## External PostgreSQL and Redis

With the `postgres` and `redis` components off, MISP connects to the servers that `DB_HOST`
and `MISP_REDIS_HOST` name. A database operator keeps the connection to its database in a
Secret. `connectionSecrets` names each such Secret and the env var that each of its keys fills:

```yaml
connectionSecrets:
  - name: misp-pguser-misp
    keys: {DB_HOST: host, DB_PORT: port, DB_NAME: dbname, DB_USER: user, DB_PASSWORD: password}
  - name: misp-redis
    keys: {MISP_REDIS_PASSWORD: password}
databaseCA: {name: misp-cluster-cert, key: ca.crt}
env:
  DB_SSLMODE: verify-full
  MISP_REDIS_HOST: misp-redis
```

The keys can fill `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`,
`MISP_REDIS_HOST`, `MISP_REDIS_PORT` and `MISP_REDIS_PASSWORD`. Each replaces the same entry of
`misp-env`, `misp-db` or `misp-app` in every pod. A var that no Secret fills keeps its value
from `env` or `secrets`. The Secrets must be in the release's namespace. A change in a Secret
does not roll the pods: restart them.

`databaseCA` names the Secret and key of the CA certificate that signed the server's
certificate. Every pod that connects mounts it, and `DB_SSLROOTCERT` names the file. Set
`DB_SSLMODE` in `env` ([configuration.md](configuration.md#database)): `verify-full` checks
the certificate and the host name, `require` only encrypts.

| Operator | Secret and keys | CA for `databaseCA` | Notes |
|---|---|---|---|
| CloudNativePG | `<cluster>-app`: `host`, `port`, `dbname`, `username`, `password` | `<cluster>-ca`, key `ca.crt` | `host` is `<cluster>-rw`, a short name: in another namespace, set `DB_HOST` in `env` to `<cluster>-rw.<namespace>.svc` |
| Percona Operator for PostgreSQL | `<cluster>-pguser-<user>`: `host`, `port`, `dbname`, `user`, `password` | `<cluster>-cluster-cert`, key `ca.crt` | Accepts password logins only over TLS: `DB_SSLMODE` `require` or `verify-full` |
| StackGres | A Secret of your own with the password of MISP's user | None: the server certificate is self-signed | The SGCluster's own Secret holds only the operator's users. Create MISP's user and database with an SGScript, and set `DB_HOST` in `env` to `<cluster>`, the Service to the leader: port 5432 through PgBouncer, 5433 direct |

The database must be owned by `DB_USER`, and a connection pooler in front of it must run in
session mode ([configuration.md](configuration.md#database)).

## Storage

| Resource | Default | Notes |
|----------|---------|-------|
| PostgreSQL | StatefulSet, 20Gi ReadWriteOnce claim (`postgres` component) | `postgres.size` and `postgres.storageClass` |
| Attachments, org logos, custom images | claim `attachments`, 20Gi ReadWriteMany | Web and worker pods share it, and `helm uninstall` leaves it. For S3, set `PLUGIN_S3_BUCKET_NAME` in `env` and `attachments.claim: false` |
| Redis | No persistence (`redis` component) | Sessions and queued jobs are lost when Redis restarts |
| Everything else | `emptyDir` per pod | `app/Config`, `app/tmp`, `.gnupg`, `app/files/{scripts/tmp,certs,terms}` |

The web and worker entrypoints refuse to start when the attachments directory is not
writable and S3 is not configured. A sync server certificate uploaded through the UI lands
on one replica only; use the `misp-certs` Secret.

## Network policies

The `networkPolicy` component allows only the paths in the diagram in
[architecture.md](architecture.md) plus DNS, HTTPS to the outside for web, worker, modules,
metrics and the task CronJobs, SMTP (ports 25, 465 and 587, in or outside the cluster)
for web, worker and the task CronJobs, and LDAP (ports 389 and 636, in or outside the
cluster) for web and the task CronJobs. A mail relay or an LDAP server on another port
needs a change to the template. `networkPolicy.ingressNamespace` (default `haproxy-controller`) and
`networkPolicy.monitoringNamespace` (default `monitoring`) name the namespaces of the
ingress and of Prometheus. The header of `deploy/chart/templates/networkpolicy.yaml`
lists every flow. Any CNI that enforces the NetworkPolicy API applies them, Cilium included.
While the `postgres` or `redis` component is off, the rules in `networkPolicy.databaseEgress`
or `networkPolicy.redisEgress` reach the external server: by default any destination on port
5432 or 6379. Narrow a rule to the server's pods with `to`, and name the port the pods listen
on, which can differ from the port of their Service.
A cluster whose CNI enforces none leaves every pod open to the namespace and, without a
default-deny elsewhere, to the cluster.

## Sizing

PHP's `memory_limit` is a ceiling per process, not a reservation: a request or a job takes
what it needs and fails once it passes the ceiling. The pod limits in `values.yaml` hold the
usual load, and the ceiling decides what happens under an unusual one.

| Pod | PHP processes | Ceiling per process | The pod limit must hold |
|-----|---------------|---------------------|-------------------------|
| web | `PHP_FCGI_CHILDREN` php-fpm children | `PHP_MEMORY_LIMIT` | the children at their usual size, plus each heavy request at the ceiling. The default limit holds two heavy requests at once; for more, raise `web.resources.phpFpm.limits.memory` or lower the ceiling |
| worker | the sum of `NUM_WORKERS_*` workers | `WORKER_MEMORY_LIMIT`, or `PHP_MEMORY_LIMIT` when unset | the idle workers (see [architecture.md](architecture.md#startup-and-footprint)) plus each heavy job at the ceiling. A job past the ceiling fails alone; without one (Debian's CLI default) it would grow until the kubelet killed the pod with every worker in it |
| configure | one `cake` process | none | `configure.resources` bounds a schema update |
| task CronJobs | one `cake` process | `WORKER_MEMORY_LIMIT`, or `PHP_MEMORY_LIMIT` when unset | `cronjobs.resources`: a pull, a feed fetch or a summary at the ceiling |

## Periodic tasks

The task runner in the main image does all periodic work. MISP's own scheduler
(`scheduler_worker`) never runs, so a task enabled under MISP's Scheduled tasks page runs
nowhere; `misp_scheduled_tasks_enabled` counts them (see [metrics.md](metrics.md)).
Manual actions in the UI and the API, such as a pull, a push or fetching one event from a
remote server, do not use a scheduler: the workers run them.

<!-- generated: periodic-tasks -->
| Task | Does | Runs | CronJob schedule (component) |
|---|---|---|---|
| `cache-feeds` | Cache every feed | `cake Server cacheFeed 1 all` | `20 2 * * *` (`cronjobs`) |
| `fetch-feeds` | Fetch every enabled feed | `cake Server fetchFeed 1 all` | `30 2 * * *` (`cronjobs`) |
| `cache-servers` | Cache the events of every server | `cake Server cacheServerAll 1` | `40 2 * * *` (`cronjobs`) |
| `update-galaxies` | Update MISP's bundled galaxies | `cake Admin updateGalaxies false` | `0 3 * * *` (`cronjobs`) |
| `update-taxonomies` | Update MISP's bundled taxonomies | `cake Admin updateTaxonomies` | `10 3 * * *` (`cronjobs`) |
| `update-warninglists` | Update MISP's bundled warninglists | `cake Admin updateWarningLists` | `20 3 * * *` (`cronjobs`) |
| `update-noticelists` | Update MISP's bundled noticelists | `cake Admin updateNoticeLists` | `30 3 * * *` (`cronjobs`) |
| `update-object-templates` | Update MISP's bundled object templates | `cake Admin updateObjectTemplates 1` | `40 3 * * *` (`cronjobs`) |
| `pull-servers` | Pull from every server with pull enabled | `cake Server pullAll 1 full` | `*/5 * * * *` (`cronjobs`) |
| `push-servers` | Push to every server with push enabled | `cake Server pushAll 1 full` | `*/15 * * * *` (`cronjobs`) |
| `push-taxii` | Push to every enabled TAXII server | `cake Server push_taxii 1 all` | `7 * * * *` (`cronjobs`) |
| `sharing-group-blueprints` | Apply the sharing group blueprints | `cake Admin executeSGBlueprint all` | `37 * * * *` (`cronjobs`) |
| `workflow` | Run one ad-hoc workflow, by its ID | `cake Workflow executeAdHocWorkflow <id>` | none |
| `periodic-summary` | Send the daily, weekly (Mondays) and monthly (the first) summaries users subscribed to | `cake Server sendPeriodicSummaryToUsers` | `0 6 * * *` (`cronjobs`) |
| `check-user-validity` | Report every account as valid or invalid at the OIDC or LDAP provider | `cake Admin checkUserValidity` | none |
| `block-invalid-users` | Disable the accounts the OIDC or LDAP provider no longer backs | `cake Admin blockInvalidUsers` | none |
<!-- end generated -->

Each task runs in its CronJob's pod: the pod renders `app/Config` and runs the console
command that MISP's own scheduler would hand a worker, as user 1, with the `misp-db` and
`misp-app` Secrets and the attachments claim. So periodic work never waits in the workers'
queues, and the workers run only what users start. A run's exit code is the console's, and its
log is the Job's. The PHP process has the workers' memory ceiling (`WORKER_MEMORY_LIMIT`, else
`PHP_MEMORY_LIMIT`), which `cronjobs.resources` must hold. The periodic summary needs a mail
relay (`SMTP_FQDN`, `SMTP_PORT`).

`cronjobs.tasks` maps each task to its schedule: change a schedule, or set it to `null` to
drop the CronJob. `cronjobs.workflows` maps a workflow ID to a schedule, one CronJob
`workflow-<id>` each. Every task CronJob pod carries `app.kubernetes.io/component:
misp-task`; the network policies select on that label. `check-user-validity` and `block-invalid-users` have no schedule, because MISP runs them
only with the oidc or ldap group: give `check-user-validity` one to report the accounts the
provider no longer backs, or `block-invalid-users` to disable them.

On demand:

```bash
kubectl -n misp create job --from=cronjob/pull-servers pull-now
```
