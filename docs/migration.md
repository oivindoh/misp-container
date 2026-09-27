# Migrating an existing MISP instance

This guide covers migrating from an existing MISP installation (official misp-docker, bare-metal, or VM) to this container image.

## TL;DR

1. Dump the old database (`mysqldump --single-transaction --hex-blob`), copy `app/files`
   attachments and, if you keep it, `.gnupg`.
2. Put the old instance's `Security.salt`, `Security.encryption_key` and `MISP.uuid` into the
   new secrets and config: without them every password, authkey and sync relationship breaks.
3. Start only the database, import the dump, copy the files in, then start the stack. The
   configure step runs `cake Admin runUpdates` for the schema gap.
4. In Kubernetes, the same order: the `mariadb` component first, restore into it, files onto the
   `attachments` claim (or S3), then the first sync.

## What migrates

| Data | How | Notes |
|------|-----|-------|
| Events, attributes, objects | MySQL dump/restore | Full fidelity |
| Organisations, users, roles | MySQL dump/restore | Passwords, authkeys preserved |
| Server sync configs | MySQL dump/restore | Authkeys, pull/push rules preserved |
| Tags, taxonomies, galaxies | MySQL dump/restore | Custom tags preserved |
| Warninglists | MySQL dump/restore | Custom warninglists preserved |
| Sharing groups | MySQL dump/restore | Membership preserved |
| File attachments | Copy to `app/attachments/` (Compose volume or the Kubernetes `attachments` claim) | Or migrate to S3 |
| GPG keys | Copy `.gnupg/` | Or generate new |
| MISP settings | MySQL dump/restore | Stored in `system_settings` table |

## What does NOT migrate

- **PHP sessions** -- users will need to log in again
- **Redis cache** -- rebuilt automatically on startup
- **CakePHP cache** -- rebuilt automatically
- **Log files** -- start fresh (logs go to stdout anyway)
- **config.php** -- regenerated from env vars (settings in DB take precedence)

## Prerequisites

- podman with a compose provider
- Access to the existing MISP database (mysqldump)
- Access to the existing MISP file system (for attachments and GPG keys)

## Step 1: Export from the existing instance

### Database dump

```bash
# On the existing MISP server
mysqldump -u root -p --single-transaction --routines --triggers \
    --hex-blob --default-character-set=utf8mb4 \
    misp > misp-backup.sql
```

`--hex-blob` encodes binary columns as hex literals instead of raw bytes, avoiding character-set corruption on import. `--default-character-set=utf8mb4` ensures multi-byte text survives the round-trip.

If using the official misp-docker:
```bash
docker compose exec db mysqldump -u root -p --single-transaction \
    --hex-blob --default-character-set=utf8mb4 \
    misp > misp-backup.sql
```

If migrating from an existing MariaDB instance (same major version), you can alternatively use `mariadb-backup` for a binary-level physical copy, which avoids text encoding entirely:
```bash
docker compose exec db mariadb-backup --backup --user=root \
    --password=<root-password> --databases=misp \
    --stream=mbstream > misp-backup.mbstream
```

### File attachments

```bash
# Copy the attachments directory
tar czf misp-files.tar.gz -C /var/www/MISP/app files/
```

If attachments are already on S3, skip this step.

### GPG keys (optional)

```bash
tar czf misp-gnupg.tar.gz -C /var/www/MISP .gnupg/
```

Skip if you want to generate new GPG keys (recommended for fresh starts).

## Step 2: Prepare the new stack

Clone this repository and configure:

```bash
git clone https://github.com/oivindoh/misp-container.git
cd misp-container/deploy
```

Edit `compose-secrets.env` with your passwords:
```bash
MYSQL_PASSWORD=<your-db-password>
MYSQL_ROOT_PASSWORD=<your-root-password>
MISP_REDIS_PASSWORD=<your-redis-password>
ADMIN_PASSWORD=<your-admin-password>
SECURITY_SALT=<salt-from-the-old-instance>
```

Edit `compose.env` with your instance URL and UUID:
```bash
MISP_BASEURL=https://your-misp.example.com
MISP_UUID=<uuid-from-the-old-instance>
```

## Step 3: Start infrastructure only

Start MySQL and Redis without MISP (so we can import the dump before MISP touches the DB):

```bash
podman compose up -d mysql redis
```

Wait for MySQL to be healthy:
```bash
podman compose exec mysql mariadb -u root -p<root-password> -e "SELECT 1"
```

## Step 4: Import the database

If you used `mysqldump`:
```bash
# Create the database if it doesn't exist
podman compose exec -T mysql mariadb -u root -p<root-password> \
    -e "CREATE DATABASE IF NOT EXISTS misp CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# Import the dump
podman compose exec -T mysql mariadb -u root -p<root-password> \
    --default-character-set=utf8mb4 misp < misp-backup.sql
```

If you used `mariadb-backup`:
```bash
# Stop the database, prepare and restore
podman compose stop mysql
podman compose exec -T mysql mbstream -x -C /var/lib/mysql < misp-backup.mbstream
podman compose exec -T mysql mariadb-backup --prepare --target-dir=/var/lib/mysql
podman compose start mysql
```

### Version check

If migrating from MISP 2.4.x, the schema upgrade will run automatically on first startup. MISP's `cake Admin runUpdates` handles this. No manual migration needed.

If migrating from a different MISP 2.5.x version, schema updates also run automatically.

## Step 5: Restore files

### Attachments (Compose only -- for Kubernetes, use the PVC or S3)

This image stores attachments in `app/attachments/` (a dedicated volume), separate from `app/files/`, which ships in the image. Image upgrades never touch your uploaded data.

```bash
# Extract into a temporary directory
mkdir -p /tmp/misp-restore
tar xzf misp-files.tar.gz -C /tmp/misp-restore

# Create the web container (and its volumes) without starting it, then copy files in
podman compose create web
podman compose cp /tmp/misp-restore/files/. web:/var/www/MISP/app/attachments/
```

In Kubernetes, copy the files onto the `attachments` claim (a temporary pod with the claim mounted and `kubectl cp`), or use S3: see [Migrating to S3 storage](#migrating-to-s3-storage) below.

### GPG keys (optional)

```bash
tar xzf misp-gnupg.tar.gz -C /tmp/misp-restore
podman compose cp /tmp/misp-restore/.gnupg/. web:/var/www/MISP/.gnupg/
```

## Step 6: Start the full stack

```bash
podman compose up -d
```

MISP will:
1. Run schema migrations if needed (`cake Admin runUpdates`)
2. Apply settings: env-driven settings are enforced, defaults from `settings.yaml` are written only where the setting is missing
3. Start PHP-FPM and background workers

## Step 7: Verify

```bash
# Check logs
podman compose logs web --tail=20

# Verify web UI
open http://localhost:8080

# Check event count
curl -sf -H "Authorization: <your-api-key>" \
    http://localhost:8080/events/index | jq length
```

### Common issues after migration

**"MISP.live is not set"** -- The configure step sets this last. Web and worker containers wait for it and retry.

**"CSRF validation failed"** -- If running multiple web replicas, ensure `SECURITY_SALT` and `MISP_UUID` are set and match the values from your old instance:
```bash
# Get from old DB
SELECT value FROM system_settings WHERE setting='Security.salt';
SELECT value FROM system_settings WHERE setting='MISP.uuid';
```
Set these in `compose-secrets.env` and `compose.env`:
```bash
SECURITY_SALT=<value-from-old-instance>
MISP_UUID=<value-from-old-instance>
```

**"GPG key not found"** -- Either restore your old `.gnupg` directory or set `AUTOCONF_GPG=true` to generate a new key. If you generate a new key, you'll need to re-export it to sync partners.

**Password doesn't work** -- The admin password from `ADMIN_PASSWORD` env var is only set on first run (when the user doesn't exist). If the user already exists in the imported DB, the env var is ignored. Use the password from your old instance.

**Workers not processing** -- Check that `SIMPLEBACKGROUNDJOBS_SUPERVISOR_HOST` is set correctly: `worker`, the service name in both Compose and Kubernetes.

## Migrating to S3 storage

If your old instance uses local file storage and you want to switch to S3:

1. Complete the migration above with local files first
2. Set up your S3 bucket (AWS, MinIO, Garage, Ceph)
3. Configure S3 in `compose.env` (the env var names follow the MISP setting names):
   ```
   PLUGIN_S3_BUCKET_NAME=misp-attachments
   PLUGIN_S3_AWS_ENDPOINT=https://s3.example.com
   PLUGIN_S3_AWS_ACCESS_KEY=...
   PLUGIN_S3_AWS_SECRET_KEY=...
   PLUGIN_S3_REGION=...
   ```
4. Migrate existing attachments to S3 using the MISP admin tool:
   ```bash
   podman compose exec web /var/www/MISP/app/Console/cake Admin migrateToS3
   ```

## Migrating from official misp-docker

The official [MISP/misp-docker](https://github.com/MISP/misp-docker) uses the same MySQL schema, so the database dump/restore works directly.

Key differences to account for:
- **User UID**: Official image runs as `www-data` (33), ours runs as UID 1000. File ownership in mounted volumes may need adjusting.
- **No .dist pattern**: Our image doesn't use the `.dist` directory sync. `app/files` ships in the image; only `scripts/tmp`, `certs`, `terms` and `img/orgs` are volumes.
- **No rsync/supervisord in web**: Workers run in a separate container, not inside the web container.
- **No root at runtime**: The entrypoint never runs as root. All file permissions are set at build time.
