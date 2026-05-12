# Migrating an existing MISP instance

This guide covers migrating from an existing MISP installation (official misp-docker, bare-metal, or VM) to this container image.

## What migrates

| Data | How | Notes |
|------|-----|-------|
| Events, attributes, objects | MySQL dump/restore | Full fidelity |
| Organisations, users, roles | MySQL dump/restore | Passwords, authkeys preserved |
| Server sync configs | MySQL dump/restore | Authkeys, pull/push rules preserved |
| Tags, taxonomies, galaxies | MySQL dump/restore | Custom tags preserved |
| Warninglists | MySQL dump/restore | Custom warninglists preserved |
| Sharing groups | MySQL dump/restore | Membership preserved |
| File attachments | Copy to `app/attachments/` | Or migrate to S3 (required for K8s) |
| GPG keys | Copy `.gnupg/` | Or generate new |
| MISP settings | MySQL dump/restore | Stored in `system_settings` table |

## What does NOT migrate

- **PHP sessions** -- users will need to log in again
- **Redis cache** -- rebuilt automatically on startup
- **CakePHP cache** -- rebuilt automatically
- **Log files** -- start fresh (logs go to stdout anyway)
- **config.php** -- regenerated from env vars (settings in DB take precedence)

## Prerequisites

- Docker and Docker Compose installed
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
REDIS_PASSWORD=<your-redis-password>
ADMIN_PASSWORD=<your-admin-password>
```

Edit `compose.env` with your instance URL:
```bash
BASE_URL=https://your-misp.example.com
```

## Step 3: Start infrastructure only

Start MySQL and Redis without MISP (so we can import the dump before MISP touches the DB):

```bash
docker compose up -d misp-mysql misp-redis
```

Wait for MySQL to be healthy:
```bash
docker compose exec misp-mysql mariadb -u root -p<root-password> -e "SELECT 1"
```

## Step 4: Import the database

If you used `mysqldump`:
```bash
# Create the database if it doesn't exist
docker compose exec -T misp-mysql mariadb -u root -p<root-password> \
    -e "CREATE DATABASE IF NOT EXISTS misp CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# Import the dump
docker compose exec -T misp-mysql mariadb -u root -p<root-password> \
    --default-character-set=utf8mb4 misp < misp-backup.sql
```

If you used `mariadb-backup`:
```bash
# Stop the database, prepare and restore
docker compose stop misp-mysql
docker compose exec -T misp-mysql mbstream -x -C /var/lib/mysql < misp-backup.mbstream
docker compose exec -T misp-mysql mariadb-backup --prepare --target-dir=/var/lib/mysql
docker compose start misp-mysql
```

### Version check

If migrating from MISP 2.4.x, the schema upgrade will run automatically on first startup. MISP's `cake Admin runUpdates` handles this. No manual migration needed.

If migrating from a different MISP 2.5.x version, schema updates also run automatically.

## Step 5: Restore files

### Attachments (Docker Compose only -- for Kubernetes, use S3)

This image stores attachments in `app/attachments/` (a dedicated volume), separate from `app/files/` which holds MISP distribution files managed by the init container. This prevents image upgrades from overwriting your uploaded data.

```bash
# Extract into a temporary directory
mkdir -p /tmp/misp-restore
tar xzf misp-files.tar.gz -C /tmp/misp-restore

# Start init container to create volumes, then copy files in
docker compose up init
docker compose cp /tmp/misp-restore/files/. web:/var/www/MISP/app/attachments/
```

For Kubernetes deployments, S3 is the only supported attachment backend. See [Migrating to S3 storage](#migrating-to-s3-storage) below.

### GPG keys (optional)

```bash
tar xzf misp-gnupg.tar.gz -C /tmp/misp-restore
docker compose cp /tmp/misp-restore/.gnupg/. web:/var/www/MISP/.gnupg/
```

## Step 6: Start the full stack

```bash
docker compose up -d
```

MISP will:
1. Run schema migrations if needed (`cake Admin runUpdates`)
2. Apply settings from env vars (won't overwrite existing DB settings unless they're `type: envar`)
3. Start PHP-FPM and background workers

## Step 7: Verify

```bash
# Check logs
docker compose logs web --tail=20

# Verify web UI
open http://localhost:8080

# Check event count
curl -sf -H "Authorization: <your-api-key>" \
    http://localhost:8080/events/index | jq length
```

### Common issues after migration

**"MISP.live is not set"** -- The web container sets this on startup. If the worker starts before the web container finishes configuration, it will wait and retry.

**"CSRF validation failed"** -- If running multiple web replicas, ensure `SALT` and `UUID` are set in your secrets and match the values from your old instance:
```bash
# Get from old DB
SELECT value FROM system_settings WHERE setting='Security.salt';
SELECT value FROM system_settings WHERE setting='MISP.uuid';
```
Set these in `compose-secrets.env`:
```bash
SALT=<value-from-old-instance>
UUID=<value-from-old-instance>
```

**"GPG key not found"** -- Either restore your old `.gnupg` directory or set `AUTOCONF_GPG=true` to generate a new key. If you generate a new key, you'll need to re-export it to sync partners.

**Password doesn't work** -- The admin password from `ADMIN_PASSWORD` env var is only set on first run (when the user doesn't exist). If the user already exists in the imported DB, the env var is ignored. Use the password from your old instance.

**Workers not processing** -- Check that `SUPERVISOR_HOST` is set correctly. In Docker Compose it should be `worker` (the service name). In Kubernetes it's `127.0.0.1` (sidecar).

## Migrating to S3 storage

If your old instance uses local file storage and you want to switch to S3:

1. Complete the migration above with local files first
2. Set up your S3 bucket (AWS, MinIO, Garage, Ceph)
3. Configure S3 in `compose.env` or via the MISP UI:
   ```
   S3_BUCKET=misp-attachments
   S3_ENDPOINT=https://s3.example.com
   S3_ACCESS_KEY=...
   S3_SECRET_KEY=...
   ```
4. Migrate existing attachments to S3 using the MISP admin tool:
   ```bash
   docker compose exec web /var/www/MISP/app/Console/cake Admin migrateToS3
   ```

## Migrating from official misp-docker

The official [MISP/misp-docker](https://github.com/MISP/misp-docker) uses the same MySQL schema, so the database dump/restore works directly.

Key differences to account for:
- **User UID**: Official image runs as `www-data` (33), ours runs as UID 1000. File ownership in mounted volumes may need adjusting.
- **No .dist pattern**: Our image doesn't use the `.dist` directory sync. Files are populated by the init container from a compressed tarball.
- **No rsync/supervisord in web**: Workers run in a separate container, not inside the web container.
- **No root at runtime**: The entrypoint never runs as root. All file permissions are set at build time.
