# MISP 3.0

The image runs MISP's `3.0_wip` branch, pinned to a commit: `ARG CORE_COMMIT` in the
`Dockerfile`. MISP has not released 3.0 yet, and the branch still changes its database schema
from week to week. Each release of this repository carries one pin, and the release notes
name it.

| Artefact | Version |
|----------|---------|
| `ghcr.io/oivindoh/misp-container`, `-caddy`, `-modules` | `3.0.0-wip.N` |
| `oci://ghcr.io/oivindoh/charts/misp` | `3.0.0-wip.N`, with the same image tag as `appVersion` |

Helm skips a pre-release version unless you name it:

```bash
helm install misp oci://ghcr.io/oivindoh/charts/misp --version 3.0.0-wip.N \
    --namespace misp --create-namespace -f values.yaml
```

## A fresh deployment

The configure step imports MISP 3.0's own baseline, `POSTGRESQL.sql`, on PostgreSQL 16 or
later. It runs `CREATE EXTENSION pg_trgm`, which works when `DB_USER` owns the database, as in
the `postgres` component.

## A MISP 2.x database

The migrate component imports a MISP 2.x database from MySQL or MariaDB: it converts a restored
copy of it with `cake Admin runUpgrade`, then copies it into PostgreSQL
([migration.md](migration.md#an-older-source)). The configure step refuses a 2.x database that
the deployment points at directly.

## A newer release

A newer release can carry a newer pin, whose schema changes are one-way: the configure step
applies them, and the only way back is a backup. Before each upgrade, back up the database and
check that the backup restores ([operations.md](operations.md#what-to-back-up)).

## Known differences from 2.5

| What | In 3.0 |
|------|--------|
| The events index on PostgreSQL | Answers 500 for a user on the Overmind theme: the stats query names the alias `Event` unquoted |
| The STIX 1 export | Needs `MISP.org`, which `ADMIN_ORG` fills |
| The HIDS, OpenIOC and Suricata exports | Gone |
