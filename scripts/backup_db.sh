#!/bin/bash
# backup_db.sh — taeglicher pg_dump der Prod-DB.
#
# Workflow:
#   1. pg_dump aus dem Postgres-Container (sql-Format, --clean --if-exists --no-owner)
#   2. gzip + Timestamp: dump-YYYY-MM-DD-HHMM.sql.gz
#   3. Lokal nach /var/backups/gewerbeagent/ (7 Tage Retention)
#   4. Optional Off-Site nach Hetzner Storage-Box wenn $BACKUP_OFFSITE gesetzt
#      ist (Format: user@u123456.your-storagebox.de:/home/backups/prod)
#
# Cron-Setup (Host, NICHT Container) — empfohlen 03:30 UTC taeglich:
#   30 3 * * * /opt/gewerbeagent/framework/scripts/backup_db.sh \
#        >> /var/log/gewerbeagent-backup.log 2>&1
#
# Restore: siehe scripts/restore_db.sh.
#
# Exit-Codes:
#   0  Backup erfolgreich (lokal + ggf. off-site)
#   1  pg_dump fehlgeschlagen
#   2  gzip fehlgeschlagen
#   3  off-site-Sync fehlgeschlagen (lokales Backup bleibt aber bestehen)
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/gewerbeagent}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-gewerbeagent_postgres}"
POSTGRES_USER="${POSTGRES_USER:-gewerbeagent}"
POSTGRES_DB="${POSTGRES_DB:-gewerbeagent}"
# Off-Site (Hetzner Storage-Box). Leer = nur lokal.
BACKUP_OFFSITE="${BACKUP_OFFSITE:-}"

ts=$(date -u +%Y-%m-%d-%H%M)
filename="dump-${POSTGRES_DB}-${ts}.sql.gz"
filepath="${BACKUP_DIR}/${filename}"

mkdir -p "$BACKUP_DIR"

echo "[$(date -u +%FT%TZ)] Backup-Start → $filepath"

# pg_dump im Container, Output via STDOUT zum Host, gzip on-the-fly.
# --clean --if-exists: Restore kann ohne Drop-DB nochmal druebergebuegelt
#   werden. --no-owner: kein OWNER-Statement (laeuft so auch auf dev-DB).
if ! docker exec -i "$POSTGRES_CONTAINER" \
        pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        --clean --if-exists --no-owner --no-privileges \
        | gzip -9 > "$filepath"; then
    echo "[$(date -u +%FT%TZ)] FEHLER: pg_dump fehlgeschlagen" >&2
    rm -f "$filepath"
    exit 1
fi

# Plausi-Check: Datei muss > 1 KB sein (leere DB-Dumps sind ~600 Byte).
size=$(stat -c %s "$filepath" 2>/dev/null || stat -f %z "$filepath")
if [[ "$size" -lt 1024 ]]; then
    echo "[$(date -u +%FT%TZ)] FEHLER: Dump nur $size Bytes — verdaechtig klein" >&2
    rm -f "$filepath"
    exit 2
fi

echo "[$(date -u +%FT%TZ)] Backup ok: ${size} Bytes"

# Retention: alte Dumps loeschen
find "$BACKUP_DIR" -maxdepth 1 -name "dump-${POSTGRES_DB}-*.sql.gz" \
    -type f -mtime "+${RETENTION_DAYS}" -delete

# Off-Site (optional)
if [[ -n "$BACKUP_OFFSITE" ]]; then
    echo "[$(date -u +%FT%TZ)] Off-Site-Sync nach $BACKUP_OFFSITE …"
    if ! rsync -e "ssh -o StrictHostKeyChecking=accept-new" \
            -aq "$filepath" "$BACKUP_OFFSITE/"; then
        echo "[$(date -u +%FT%TZ)] WARN: Off-Site-Sync fehlgeschlagen, lokal bleibt erhalten" >&2
        exit 3
    fi
    echo "[$(date -u +%FT%TZ)] Off-Site ok"
fi

echo "[$(date -u +%FT%TZ)] Backup-Ende → $filename ($(du -h "$filepath" | cut -f1))"
