#!/bin/bash
# restore_db.sh — restore eines Backup-Dumps in eine DB.
#
# WICHTIG: --clean --if-exists im Dump bedeutet, dass ZIEL-Tabellen
# GELOESCHT werden. NIE direkt gegen die Prod-DB laufen lassen ohne
# vorher eine zusaetzliche Restore-DB anzulegen!
#
# Empfohlener Workflow:
#   1. Restore-DB anlegen: docker exec gewerbeagent_postgres \
#        psql -U gewerbeagent -c "CREATE DATABASE gewerbeagent_restore"
#   2. Skript ausfuehren mit --db=gewerbeagent_restore
#   3. Verifikation, dann manuell die Daten extrahieren oder DBs swappen
#
# Verwendung:
#   ./scripts/restore_db.sh --file=/var/backups/.../dump-XYZ.sql.gz \
#       --db=gewerbeagent_restore
#
# Exit-Codes:
#   0  Restore erfolgreich
#   1  Argument fehlt oder Datei nicht gefunden
#   2  psql fehlgeschlagen
set -euo pipefail

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-gewerbeagent_postgres}"
POSTGRES_USER="${POSTGRES_USER:-gewerbeagent}"

FILE=""
DB=""

# Args parsen
while [[ $# -gt 0 ]]; do
    case "$1" in
        --file=*) FILE="${1#--file=}" ;;
        --db=*)   DB="${1#--db=}" ;;
        -h|--help)
            echo "Usage: $0 --file=<dump.sql.gz> --db=<target_db>"
            exit 0
            ;;
        *)
            echo "Unbekanntes Arg: $1" >&2
            exit 1
            ;;
    esac
    shift
done

if [[ -z "$FILE" || -z "$DB" ]]; then
    echo "FEHLER: --file und --db sind beide Pflicht" >&2
    exit 1
fi

if [[ ! -f "$FILE" ]]; then
    echo "FEHLER: Dump-Datei nicht gefunden: $FILE" >&2
    exit 1
fi

# Sicherheits-Sperre: niemals Prod-DB direkt ueberschreiben.
if [[ "$DB" == "gewerbeagent" || "$DB" == "gewerbeagent_prod" ]]; then
    echo "FEHLER: Restore in '$DB' ist gesperrt — bitte in eine Test-DB" >&2
    echo "Anlegen: docker exec $POSTGRES_CONTAINER psql -U $POSTGRES_USER \\"
    echo "         -c \"CREATE DATABASE gewerbeagent_restore\"" >&2
    exit 1
fi

echo "[$(date -u +%FT%TZ)] Restore $FILE → DB $DB"

# Existiert die DB?
if ! docker exec "$POSTGRES_CONTAINER" \
        psql -U "$POSTGRES_USER" -lqt | cut -d\| -f1 | grep -qw "$DB"; then
    echo "FEHLER: Datenbank '$DB' existiert nicht. Erst anlegen:" >&2
    echo "  docker exec $POSTGRES_CONTAINER psql -U $POSTGRES_USER \\"
    echo "    -c \"CREATE DATABASE $DB\"" >&2
    exit 1
fi

# Restore: zcat → psql im Container
if ! zcat "$FILE" | docker exec -i "$POSTGRES_CONTAINER" \
        psql -U "$POSTGRES_USER" -d "$DB" --quiet --set ON_ERROR_STOP=on; then
    echo "[$(date -u +%FT%TZ)] FEHLER: Restore fehlgeschlagen" >&2
    exit 2
fi

# Plausi: Tabellen-Anzahl zaehlen
count=$(docker exec "$POSTGRES_CONTAINER" \
    psql -U "$POSTGRES_USER" -d "$DB" -tAc \
    "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
echo "[$(date -u +%FT%TZ)] Restore ok — $count Tabellen in $DB"
