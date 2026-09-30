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

## Client addresses

MISP logs the first address in `X-Forwarded-For` as the client of each request. The
`ingress-haproxy` component replaces that header with the address HAProxy sees, so a client
cannot write its own address into the audit log. Another ingress or Gateway must do the same.
The caddy sidecar keeps the header only from a trusted proxy, `TRUSTED_PROXY_CIDR`: every
private range in Kubernetes, where the ingress pods live, and none in Compose, where clients
reach caddy directly. With `netpol-cilium`, only the ingress reaches the web pods.

## Secrets

Three Secrets follow their consumers; the base generates them from `.env` files that Compose
reads too:

| Secret | File | Keys | Who gets it |
|--------|------|------|-------------|
| `misp-db` | `secrets-db.env` | `DB_USER`, `DB_PASSWORD`, `MYSQL_ROOT_PASSWORD` (mariadb only) | configure, web, worker, org-sync, housekeeping, console task CronJobs, the mariadb or postgres component; metrics gets user and password only |
| `misp-app` | `secrets-app.env` | `MISP_REDIS_PASSWORD`, `GNUPG_PASSWORD`, `SECURITY_ENCRYPTION_KEY`, `SECURITY_SALT` | configure, web, worker, redis, console task CronJobs; metrics gets `MISP_REDIS_PASSWORD` only |
| `misp-admin` | `secrets-admin.env` | `ADMIN_PASSWORD`, `ADMIN_KEY` | configure, org-sync, cronjobs. With `ADMIN_KEY` empty MISP generates a key, org-sync exits without changes, and the cronjobs fail with a clear message |
| `misp-migrate` | `components/migrate/secrets-migrate.env` | `MIGRATE_SOURCE_*`, `MIGRATE_FORCE`, `MIGRATE_REPLACE_COPY` | The migrate Job only |

The base files hold placeholders that the configure Job refuses. Supply the real Secrets
from your overlay through KSOPS, as `deploy/overlays/prod` does: an encrypted
`secrets.sops.yaml` with the three Secrets and `kustomize.config.k8s.io/behavior: replace`.
Kustomize's `secretGenerator` cannot decrypt, so do not encrypt the `.env` files in place.

Two more Secrets are optional and copied into every pod at start:

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

| Task | Does | Kind | CronJob schedule |
|------|------|------|------------------|
| `pull-servers` | Pull from every server with pull enabled | API | every 5 minutes |
| `push-servers` | Push to every server with push enabled | API | every 15 minutes |
| `cache-servers` | Cache the events of every server | API | 02:40 |
| `fetch-feeds` | Fetch every enabled feed | API | 02:30 |
| `cache-feeds` | Cache every feed | API | 02:20 |
| `push-taxii` | Push to every enabled TAXII server | API | hourly |
| `sharing-group-blueprints` | Apply the sharing group blueprints | API | hourly |
| `update-galaxies`, `update-taxonomies`, `update-warninglists`, `update-noticelists`, `update-object-templates` | Update MISP's bundled definitions | API | 03:00 to 03:40 |
| `periodic-summary` | Send the daily, weekly (Mondays) and monthly (the first) summaries users subscribed to | console | 06:00, no retry |
| `check-user-validity` | Report every account as valid or invalid at the OIDC or LDAP provider | console | 05:30 (`user-validity` component) |
| `block-invalid-users` | Disable the accounts the provider no longer backs | console | patch `user-validity` to it |
| `workflow <id>` | Run one ad-hoc workflow | API | an overlay adds the CronJob |

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
