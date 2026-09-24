#!/usr/bin/env bash
set -euo pipefail
while true; do
  hour="${BACKUP_HOUR:-02}"
  if [ "$(date +%H)" = "$hour" ]; then
    file="/backups/audience_hub-$(date +%Y%m%d-%H%M).dump"
    if pg_dump -Fc -f "$file" && pg_restore --list "$file" >/dev/null; then
      echo "backup complete: $(basename "$file")"
      find /backups -name 'audience_hub-*.dump' -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
    else
      echo "backup failed" >&2
      rm -f "$file"
    fi
    sleep 3700
  else
    sleep 300
  fi
done