# Operations

## TL;DR

- Back up the database and the attachments claim; Redis holds nothing that a restart does not
  lose anyway. Keep the Secrets with the backup: without the same salt, encryption key and
  UUID the copy is unusable.
- Restore into a release with the same Secrets: load the dump, copy the attachments, then
  run the configure Job again.
- The salt and the encryption key do not rotate. The GPG key rotates through its Secret.
- A new PostgreSQL major version cannot read the data of the one before: move it with a dump
  and a restore.
- While Keycloak is down, turn OIDC off and log in with a break-glass account.

## What to back up

| Data | Where | How |
|------|-------|-----|
| The database | The `postgres` component's claim, or the external database | A logical dump (below), or a volume snapshot of the claim |
| Attachments, org logos, custom images | The claim `attachments`, or the S3 bucket | A volume snapshot, a copy of the directory, or the bucket's own versioning |
| The Secrets | `misp-db`, `misp-app`, `misp-admin`, `misp-gnupg`, `misp-certs` | Whatever manages them (SOPS, External Secrets); the backup is useless without `SECURITY_SALT`, `SECURITY_ENCRYPTION_KEY` and `MISP_UUID` |
| The values | `values.yaml`, `orgs.yaml` | Version control |

Redis needs no backup: sessions and queued jobs do not survive a restart either way. The
per-pod volumes (`app/Config`, `app/tmp`, `.gnupg`, `app/files/{scripts/tmp,certs,terms}`)
are rendered or copied in at every start.

A logical dump, from the database pod of the release:

```bash
kubectl -n misp exec sts/postgres -- pg_dump --clean --if-exists -U misp misp > misp.sql
```

The dump drops each table before it creates it, so it loads over the empty MISP that the
configure Job creates.

An external database is dumped the same way from wherever it runs. The attachments, when
the storage class offers no snapshots:

```bash
kubectl -n misp cp "$(kubectl -n misp get pod -l app.kubernetes.io/name=web -o name | head -1 | cut -d/ -f2):/var/www/MISP/app/attachments" ./attachments -c php-fpm
```

## Restore

Into a release that carries the same Secrets, in this order:

1. Install the chart with the web and worker Deployments scaled to zero
   (`web.replicas: 0`, `worker.replicas: 0`) and the same `values.yaml` otherwise. The configure
   Job creates an empty MISP.
2. Load the dump into the database: it replaces the empty tables.

   ```bash
   kubectl -n misp exec -i sts/postgres -- psql -U misp misp < misp.sql
   ```

3. Copy the attachments onto the claim, or point `PLUGIN_S3_BUCKET_NAME` at the bucket.
4. Upgrade the release with the replicas back. The configure Job runs MISP's schema updates on
   the copy and records the image version; the pods then serve.

A MISP on MySQL or MariaDB is copied by the `migrate` component instead
([migration.md](migration.md)); it reads the source database and its attachments directly.

## Keys and secrets

| Secret | Rotation |
|--------|----------|
| `SECURITY_SALT` | None. MISP hashes every password with it; a new salt invalidates every password, so users would reset them through the forgotten-password flow or an admin |
| `SECURITY_ENCRYPTION_KEY` | MISP encrypts the authkeys of sync servers and other stored credentials with it. A new key leaves the stored values unreadable: re-enter them after the change |
| `MISP_UUID` | None. Sync partners know the instance by it |
| `ADMIN_PASSWORD`, `ADMIN_KEY` | Change the value in `misp-admin` and upgrade: the configure Job sets both on user 1 |
| `MISP_REDIS_PASSWORD`, `DB_PASSWORD` | Change the value in the Secret and on the service it names, then upgrade: the checksum rolls every pod that reads it |
| The GPG key (`misp-gnupg`) | Replace `private.asc` in the Secret and upgrade: each pod imports the key of the Secret at start. Sync partners need the new public key |
| The sync server certificates (`misp-certs`) | Replace the file in the Secret and upgrade |

## Break-glass accounts

A break-glass account is a `users` entry in `orgs.yaml` with a `password`
([org-sync.md](org-sync.md#users)). It keeps MISP open to an administrator while Keycloak is
down. With `OIDC_MIXEDAUTH=false` the password form is closed, so open it this way:

1. Set `OIDC_ENABLE=false` in `env` and upgrade the release. The web pods start without
   OidcAuth and show the password form.
2. Log in with the break-glass account.
3. Once Keycloak answers again, set `OIDC_ENABLE=true` and upgrade the release.

API keys work throughout. `OIDC_MIXEDAUTH=true` keeps the password form open beside Keycloak
at all times instead, which leaves every local password open to guessing.

## Upgrades

[architecture.md](architecture.md#rollout-order) describes what serves when during a rollout.
[kubernetes.md](kubernetes.md#install) has the chart versions, and the steps for an upgrade
from chart 1.x.

### A new PostgreSQL major version

A PostgreSQL major version cannot read the data of the one before. The `postgres` pod
refuses to start on it. Move the data with a dump:

1. Dump the database with the old release, as in [What to back up](#what-to-back-up).
2. Delete the StatefulSet and its claim:

   ```bash
   kubectl -n misp delete statefulset postgres
   kubectl -n misp delete pvc postgres-data-postgres-0
   ```

3. Upgrade to the new release with `web.replicas: 0` and `worker.replicas: 0`, then follow
   [Restore](#restore) from step 2. The StatefulSet gets an empty claim, and the new major
   version creates its cluster there.

