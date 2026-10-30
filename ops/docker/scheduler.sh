#!/bin/sh
# Periodic jobs: mark lost scans every minute; purge expired data and back up daily.
# A failing job is logged and retried; it never stops the loop (the sweep must keep running).
set -u
last_daily=""
retry_after=0
while true; do
  python web/manage.py sweep_scans || echo "scheduler: ERROR sweep_scans failed" >&2
  today=$(date -u +%Y-%m-%d)
  now=$(date +%s)
  if [ "$today" != "$last_daily" ] && [ "$now" -ge "$retry_after" ]; then
    if python web/manage.py purge_expired_data && python web/manage.py backup_database --out /backups; then
      find /backups -name 'accessguard-*' -mtime +14 -delete
      last_daily=$today
    else
      echo "scheduler: ERROR daily purge or backup failed; retrying in 15 minutes" >&2
      retry_after=$((now + 900))
    fi
  fi
  sleep 60
done
