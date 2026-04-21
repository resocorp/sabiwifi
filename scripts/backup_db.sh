#!/bin/bash
# Daily Postgres backup for SabiWiFi.
# Runs as postgres user via cron. Keeps 30 days of local dumps.
# Offsite sync hook: if /opt/sabiwifi/scripts/backup_offsite.sh exists and is
# executable, it is invoked with the dump path as $1 after a successful dump.

set -euo pipefail

DB_NAME="sabiwifi"
BACKUP_DIR="/var/backups/sabiwifi"
RETENTION_DAYS=30
LOG_FILE="/var/log/sabiwifi-backup.log"

timestamp=$(date +%Y-%m-%d_%H%M%S)
dump_file="${BACKUP_DIR}/sabiwifi-${timestamp}.dump"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

log "START backup of ${DB_NAME}"

# Custom format (-Fc) is compressed and restorable via pg_restore.
# --no-owner/--no-privileges keeps the dump portable across environments.
if pg_dump -Fc --no-owner --no-privileges -f "$dump_file" "$DB_NAME" 2>>"$LOG_FILE"; then
    size=$(du -h "$dump_file" | cut -f1)
    log "OK dumped ${dump_file} (${size})"
else
    log "FAIL pg_dump exited non-zero; removing partial file"
    rm -f "$dump_file"
    exit 1
fi

# Offsite hook (no-op if not installed)
offsite_hook="/opt/sabiwifi/scripts/backup_offsite.sh"
if [[ -x "$offsite_hook" ]]; then
    if "$offsite_hook" "$dump_file" >>"$LOG_FILE" 2>&1; then
        log "OK offsite sync"
    else
        log "WARN offsite sync failed"
    fi
fi

# Prune dumps older than RETENTION_DAYS
deleted=$(find "$BACKUP_DIR" -maxdepth 1 -name 'sabiwifi-*.dump' -mtime +${RETENTION_DAYS} -print -delete | wc -l)
if [[ "$deleted" -gt 0 ]]; then
    log "OK pruned ${deleted} old dump(s)"
fi

log "END backup"
