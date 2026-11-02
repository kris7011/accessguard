#!/bin/sh
# Daily off-site copy of the database dumps the scheduler writes to /backups (read-only here).
# restic encrypts on this server before anything leaves it; the repository (SFTP to a storage
# box or S3-compatible object storage) and its password come from the environment.
# Order of a run: the newest dump must be younger than 26 hours (so a stalled scheduler is
# noticed too), back up, apply retention, verify the repository on Sundays, then ping the
# optional heartbeat URL. A missing ping is what raises the alert (see docs/deployment.md).
# A failing run logs a line starting with "offsite-backup: ERROR" and is retried in 30 minutes.
set -u
last_run=""
retry_after=0
while true; do
  today=$(date -u +%Y-%m-%d)
  now=$(date +%s)
  # After 02:00 UTC, when the scheduler's daily dump (taken after midnight UTC) exists.
  if [ "$today" != "$last_run" ] && [ "$(date -u +%H)" -ge 2 ] && [ "$now" -ge "$retry_after" ]; then
    if [ -z "$(find /backups -name 'accessguard-*' -mmin -1560)" ]; then
      echo "offsite-backup: ERROR no database dump younger than 26 hours in /backups" >&2
      retry_after=$((now + 1800))
    elif restic backup --host accessguard --tag database /backups \
      && restic forget --host accessguard --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune; then
      if [ "$(date -u +%u)" = 7 ] && ! restic check; then
        echo "offsite-backup: ERROR restic check failed" >&2
      elif [ -n "${ACCESSGUARD_BACKUP_HEARTBEAT_URL:-}" ]; then
        wget -q -O /dev/null -T 15 "$ACCESSGUARD_BACKUP_HEARTBEAT_URL" \
          || echo "offsite-backup: WARNING heartbeat ping failed" >&2
      fi
      last_run=$today
    else
      echo "offsite-backup: ERROR restic backup or retention failed; retrying in 30 minutes" >&2
      retry_after=$((now + 1800))
    fi
  fi
  sleep 300
done
