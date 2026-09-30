# Kubernetes

## TL;DR

- `deploy/base` is MISP itself; an overlay adds the components it needs (database, cache,
  ingress, network policies, CronJobs) and its own values.
- Supply the `misp-db`, `misp-app` and `misp-admin` Secrets from the overlay, for example
  through KSOPS; the base's placeholders make the configure Job refuse to run.
- Attachments need a ReadWriteMany claim or S3. The task runner in CronJobs does all
  periodic work.

## Layout

```
deploy/
  base/          # MISP itself: Jobs, Deployments, Services, config, attachments claim
  components/    # Optional parts an overlay opts into
  overlays/
    prod/        # Overlay with KSOPS secrets and every component
```

| Component | Content | Leave it out when |
|-----------|---------|-------------------|
| `mariadb` | MariaDB StatefulSet with a 20Gi PVC | You use `postgres` or an external database (`DB_HOST`) |
| `postgres` | PostgreSQL 17 StatefulSet with a 20Gi PVC | You use `mariadb` or an external database |
| `redis` | Deployment without persistence | You run an external Redis (`MISP_REDIS_HOST`) |
| `ingress-haproxy` | Ingress for the haproxy class | Another ingress or a Gateway routes to the `web` Service |
| `gateway-api` | HTTPRoute for a Gateway API implementation; patch its parent Gateway and hostname | The `ingress-haproxy` component or your own route reaches the `web` Service |
| `netpol-cilium` | CiliumNetworkPolicies for every pod | The cluster does not run Cilium |
| `cronjobs` | The periodic task CronJobs, see [Periodic tasks](#periodic-tasks) | You want no periodic work |
| `user-validity` | A daily check of every account against the OIDC or LDAP identity provider | Neither the `oidc` nor the `ldap` group is enabled |
| `housekeeping` | Nightly deletes in `jobs`, `logs`, `audit_logs` (`HOUSEKEEPING_<TABLE>_DAYS`) | Retention is handled elsewhere |
| `pdb` | PodDisruptionBudgets for web and worker | One replica of each |
| `migrate` | One-off Job copying an existing MySQL/MariaDB MISP into the database, see [migration.md](migration.md) | Always, once the Job has run |

An overlay lists the base, the components it wants, and its own values:

```yaml
# your-overlay/kustomization.yaml
resources:
  - ../../base
components:
  - ../../components/mariadb
  - ../../components/redis
  - ../../components/ingress-haproxy
configMapGenerator:
  - name: misp-env
    behavior: merge
    literals:
      - MISP_BASEURL=https://misp.example.com
      - ADMIN_EMAIL=admin@example.com
```

Set `images` in the same kustomization that lists the components, so a tag of your choice
reaches the components' CronJobs and Jobs too; each component carries the release's tag in
its own manifests.

## Client addresses

MISP logs the first address in `X-Forwarded-For` as the client of each request. The caddy
sidecar sends it one address: the rightmost `X-Forwarded-For` address outside the trusted
proxies, `TRUSTED_PROXY_CIDR`, or the address of the peer when the peer is no trusted proxy.
When every address in the header is trusted, it is the peer's. So an address a client wrote
into the header never reaches the audit log, whether the proxy in front replaces the header
(the `ingress-haproxy` component does) or appends to it (as a Gateway does).

| Setup | `TRUSTED_PROXY_CIDR` | MISP logs |
|-------|----------------------|-----------|
| Kubernetes, unset | every private range | a client with a public address; for a client with a private address, the ingress or Gateway pod |
| Kubernetes, set in `misp-env` to the ingress or Gateway proxy pods | those pods | every client |
| Compose | `127.0.0.1/32` on the caddy service: clients reach caddy directly | every client |

With `netpol-cilium`, only the ingress reaches the web pods. For a Gateway, replace the
policy's ingress namespace (`haproxy-controller`) with the namespace of the Gateway's proxies.

## Secrets

The base generates its Secrets from `.env` files that Compose reads too, and the `migrate`
component adds `misp-migrate`. A workload gets a whole Secret, or only the keys the table
names:

<!-- generated: secrets -->
| Secret | File | Keys | Read by |
|---|---|---|---|
| `misp-db` | `deploy/base/secrets-db.env` | `DB_USER`, `DB_PASSWORD`, `MYSQL_ROOT_PASSWORD` | configure, cronjobs, housekeeping, mariadb (`DB_PASSWORD`, `DB_USER`, `MYSQL_ROOT_PASSWORD`), metrics (`DB_PASSWORD`, `DB_USER`), migrate, org-sync, postgres (`DB_PASSWORD`, `DB_USER`), user-validity, web, worker |
| `misp-app` | `deploy/base/secrets-app.env` | `MISP_REDIS_PASSWORD`, `GNUPG_PASSWORD`, `SECURITY_ENCRYPTION_KEY`, `SECURITY_SALT` | configure, cronjobs, metrics (`MISP_REDIS_PASSWORD`), migrate, redis (`MISP_REDIS_PASSWORD`), user-validity, web, worker |
| `misp-admin` | `deploy/base/secrets-admin.env` | `ADMIN_PASSWORD`, `ADMIN_KEY` | configure, cronjobs (`ADMIN_KEY`), org-sync |
| `misp-migrate` | `deploy/components/migrate/secrets-migrate.env` | `MIGRATE_SOURCE_HOST`, `MIGRATE_SOURCE_USER`, `MIGRATE_SOURCE_PASSWORD`, `MIGRATE_FORCE`, `MIGRATE_REPLACE_COPY`, `MIGRATE_SOURCE_FILES`, `MIGRATE_SOURCE_S3_BUCKET`, `MIGRATE_SOURCE_S3_ENDPOINT`, `MIGRATE_SOURCE_S3_REGION`, `MIGRATE_SOURCE_S3_ACCESS_KEY`, `MIGRATE_SOURCE_S3_SECRET_KEY` | migrate |
<!-- end generated -->

With `ADMIN_KEY` empty, MISP generates a key, org-sync exits without changes, and the API
task CronJobs fail with a clear message.

The base files hold placeholders that the configure Job refuses. Supply the real Secrets
from your overlay through KSOPS, as `deploy/overlays/prod` does: an encrypted
`secrets.sops.yaml` with the base's Secrets and `kustomize.config.k8s.io/behavior: replace`.
Kustomize's `secretGenerator` cannot decrypt, so do not encrypt the `.env` files in place.

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

| Resource | Base default | Notes |
|----------|--------------|-------|
| MariaDB or PostgreSQL | StatefulSet, 20Gi ReadWriteOnce PVC (`mariadb` or `postgres` component) | Patch the size or storage class in the overlay |
| Attachments, org logos, custom images | PVC `attachments`, 20Gi ReadWriteMany | Web and worker pods share it. For S3, set `PLUGIN_S3_BUCKET_NAME` and remove the claim in the overlay |
| Redis | No persistence (`redis` component) | Sessions and queued jobs are lost when Redis restarts |
| Everything else | `emptyDir` per pod | `app/Config`, `app/tmp`, `.gnupg`, `app/files/{scripts/tmp,certs,terms}` |

The web and worker entrypoints refuse to start when the attachments directory is not
writable and S3 is not configured. A sync server certificate uploaded through the UI lands
on one replica only; use the `misp-certs` Secret.

## Network policies

The `netpol-cilium` component allows only the paths in the diagram in [architecture.md](architecture.md) plus DNS, HTTPS to
the outside for web, worker, modules, metrics and the console tasks, and SMTP (ports 25, 465
and 587, in or outside the cluster) for web, worker and the console tasks. A mail relay on
another port needs a patch. The ingress controller namespace
(`haproxy-controller`) and the Prometheus namespace (`monitoring`) are its patch points. The
file header of `deploy/components/netpol-cilium/networkpolicy.yaml` lists every flow.

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
| `check-user-validity` | Report every account as valid or invalid at the OIDC or LDAP provider | console | `30 5 * * *` (`user-validity`) |
| `block-invalid-users` | Disable the accounts the OIDC or LDAP provider no longer backs | console | none |
<!-- end generated -->

API tasks call the MISP API with `ADMIN_KEY`, which must be set. Before dispatching, a run
reads the queue depth (`misp_jobs_queued`, which the metrics exporter reads from MISP's job
queues in Redis) and dispatches nothing while `TASK_MAX_QUEUED` jobs (default 200) or more
are waiting or running, so a slow worker pool does not pile up work. An unreachable exporter
or Redis does not block dispatch. A partner that refuses a pull or push is logged and shows
in the metrics; a run exits 1 only when no call succeeded.

Console tasks have no API. The pod renders `app/Config` and runs MISP's console, with the
configure Job's volumes and the `misp-db` and `misp-app` Secrets. The periodic summary
needs a mail relay (`SMTP_FQDN`, `SMTP_PORT`).

Every task CronJob pod carries `app.kubernetes.io/component: misp-task` (API) or
`misp-console-task` (console); the network policies select on that label. A CronJob an
overlay adds, for one server or one workflow, copies a CronJob of its kind and keeps the
label. To disable users instead of reporting them:

```yaml
# overlay: with the user-validity component
patches:
  - target: {kind: CronJob, name: user-validity}
    patch: |-
      - op: replace
        path: /spec/jobTemplate/spec/template/spec/containers/0/command/5
        value: block-invalid-users
```

On demand:

```bash
kubectl -n misp create job --from=cronjob/pull-servers pull-now
podman compose run --rm --no-deps sync python3 -m misp_container.task pull-servers   # Compose, API task
podman compose exec worker python3 -m misp_container.task periodic-summary          # Compose, console task
```
