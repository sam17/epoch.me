# epoch.me — DB backup service

Self-contained container that backs up the Postgres DB to **local disk + Cloudflare R2** on a daily loop. Built because the DB (life-metrics, ~21 MB) previously had **zero backups**.

## What it does
- `pg_dump` the DB → gzip → `/backups/epoch-<timestamp>.sql.gz` (mounted to a host dir)
- `rclone copy` the dump to the R2 bucket `epoch-me/backups/`
- Retention: **local 30 days, R2 90 days**
- Repeats every `INTERVAL_SECONDS` (default 86400 = daily); also runs once on start

## Config
- **`.env`** (copy from `.env.example`, gitignored): `DATABASE_URL` (incl. db name), optional `INTERVAL_SECONDS`
- **rclone config** mounted read-only at `/root/.config/rclone/rclone.conf` with an `[r2]` remote:
  ```ini
  [r2]
  type = s3
  provider = Cloudflare
  access_key_id = <R2 access key id>
  secret_access_key = <R2 secret>
  endpoint = https://<ACCOUNT_ID>.r2.cloudflarestorage.com
  acl = private
  no_check_bucket = true
  ```
  (R2 occasionally returns `501 NotImplemented` on an S3 op; rclone auto-retries and succeeds — harmless.)

## Run standalone (current, pointed at any DB)
```bash
docker build -t epoch-backup:latest .
docker run -d --name epoch-backup --restart unless-stopped \
  --env-file .env \
  -v /opt/backups/fxlifesheet:/backups \
  -v ~/.config/rclone/rclone.conf:/root/.config/rclone/rclone.conf:ro \
  epoch-backup:latest
```

## As a compose service (when folded into the stack; dumps the local `db`)
```yaml
  backup:
    build: ./backup
    image: epoch-backup:latest
    restart: unless-stopped
    env_file: ./backup/.env           # DATABASE_URL -> postgres://…@db:5432/<db>
    volumes:
      - ./backups:/backups
      - ~/.config/rclone/rclone.conf:/root/.config/rclone/rclone.conf:ro
    depends_on: [db]
```

## Restore
```bash
gunzip -c epoch-<timestamp>.sql.gz | psql "$DATABASE_URL"
```
