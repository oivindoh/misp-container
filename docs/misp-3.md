# MISP 3.0 Early Access

The `misp-3.0` branch builds MISP 3.0 Early Access from MISP's `3.0_wip` branch, pinned to a
commit. It is not for production: run it on a test instance, with a copy of your data. Each
push to the branch publishes the images and the chart, after every suite has passed.

| Artefact | Version |
|----------|---------|
| `ghcr.io/oivindoh/misp-container`, `-caddy`, `-modules` | `3.0-wip-<MISP commit>-<commit>`, which never moves, and `3.0-wip`, the newest |
| `oci://ghcr.io/oivindoh/charts/misp` | `3.0.0-wip.<run>.<attempt>`, with the exact image tag as `appVersion` |

Helm skips a pre-release version unless you name it:

```bash
helm install misp oci://ghcr.io/oivindoh/charts/misp --version 3.0.0-wip.<run>.<attempt> \
    --namespace misp --create-namespace -f values.yaml
```

## A fresh deployment

Install the chart as above, or set `MISP_IMAGE_TAG=3.0-wip` for Compose. The configure step
imports MISP 3.0's own baseline: `MARIADB.sql` on MariaDB 10.10 or later, `POSTGRESQL.sql` on
PostgreSQL 16 or later. The PostgreSQL baseline runs `CREATE EXTENSION pg_trgm`, which works
when `DB_USER` owns the database, as in the `postgres` component.

## Upgrading a 2.5 instance

MISP 3.0 stores each distinct attribute value once. Converting a 2.5 database to that
layout is one-way and offline: `cake Admin runUpgrade` takes MISP offline, applies every
pending update and moves the values into the new table. On a large instance it runs for
hours. The only way back is a backup taken before.

The upgrade step runs that conversion before the configure step. It leaves a converted or an
empty database alone, so a second run does nothing. The configure step of a 3.0 image
refuses a database with 2.5's layout and names the upgrade step.

With the chart:

1. Back up the database and check that the backup restores (see
   [operations.md](operations.md#what-to-back-up)).
2. Upgrade the release to a 3.0 chart with `upgrade.enabled: true` and
   `upgrade.backupConfirmed: true`. While the component is on, web and worker run 0 replicas,
   the CronJobs are suspended and the org sync does not run. The configure Job converts the
   database in its `upgrade` init container, then configures MISP. Follow it with
   `kubectl logs -f job/configure-<revision> -c upgrade`.
3. When the configure Job has completed, upgrade the release again with
   `upgrade.enabled: false`. Web and worker start, and the workers rebuild the correlations.

A failed conversion fails the configure Job without a retry and leaves MISP offline. Fix
what the log names, then upgrade the release again: the new revision's Job carries on where
the conversion stopped. With plain Helm, leave out `--wait`, or give it a timeout longer
than the conversion. With the `migrate` component on as well, the configure Job first copies
a 2.5 database from another host, then converts the copy.

With Compose:

1. Back up the database and check that the backup restores.
2. Stop web and worker: `podman compose stop web worker`.
3. Convert the database:
   `MISP_IMAGE_TAG=3.0-wip UPGRADE_BACKUP_CONFIRMED=true podman compose --profile upgrade run --rm upgrade`.
4. Start the stack on the 3.0 images: `MISP_IMAGE_TAG=3.0-wip podman compose up -d`.

| Exit code of the upgrade step | Meaning |
|-------------------------------|---------|
| 0 | Converted, or the database needs no conversion |
| 1 | `runUpgrade` stopped; MISP stays offline until a run finishes |
| 2 | `UPGRADE_BACKUP_CONFIRMED` is not `true` |
