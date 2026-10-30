#!/bin/sh
# Periodic jobs: mark lost scans every minute; purge expired data and back up daily.
set -eu
last_daily=""
while true; do
  python web/manage.py sweep_scans
  today=$(date -u +%Y-%m-%d)
  if [ "$today" != "$last_daily" ]; then
    python web/manage.py purge_expired_data
    python web/manage.py backup_database --out /backups
    find /backups -name 'accessguard-*' -mtime +14 -delete
    last_daily=$today
  fi
  sleep 60
done
