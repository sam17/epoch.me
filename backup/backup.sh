#!/usr/bin/env bash
# epoch.me DB backup loop — runs inside the epoch-backup container.
# Dumps the Postgres DB -> local /backups + Cloudflare R2, on a daily interval.
# Config via env: DATABASE_URL (required), INTERVAL_SECONDS (default 86400).
# R2 access via a mounted rclone config with an [r2] remote (see README).
set -uo pipefail

echo "[epoch-backup] started $(date); interval=${INTERVAL_SECONDS:-86400}s"
while true; do
  TS=$(date +%Y%m%d-%H%M%S)
  OUT="/backups/epoch-${TS}.sql.gz"
  if pg_dump "$DATABASE_URL" --no-owner --no-privileges 2>/tmp/err | gzip > "$OUT"; then
    sz=$(stat -c%s "$OUT")
    if [ "$sz" -gt 10000 ]; then
      rclone copy "$OUT" r2:epoch-me/backups/ && echo "[$(date)] OK $OUT ($sz b) -> R2"
      # retention: local 30 days, R2 90 days
      find /backups -name "epoch-*.sql.gz" -mtime +30 -delete
      rclone delete r2:epoch-me/backups/ --min-age 90d 2>/dev/null || true
    else
      echo "[$(date)] dump too small ($sz b) - skipping upload"; rm -f "$OUT"
    fi
  else
    echo "[$(date)] pg_dump FAILED: $(cat /tmp/err)"; rm -f "$OUT"
  fi
  sleep "${INTERVAL_SECONDS:-86400}"
done
