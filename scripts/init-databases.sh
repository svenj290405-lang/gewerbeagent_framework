#!/bin/bash
# Postgres-Init-Hook: legt zusaetzlich die Dev-DB an.
#
# Wird beim allerersten Start des Postgres-Volumes ausgefuehrt
# (docker-entrypoint-initdb.d/). Bei bestehendem Volume nicht — dann
# manuell:
#   docker compose -p prod exec postgres psql -U gewerbeagent -c \
#     "CREATE DATABASE gewerbeagent_dev OWNER gewerbeagent;"
#
# Die Prod-DB 'gewerbeagent' wird durch POSTGRES_DB im Compose-File
# automatisch von Postgres selbst angelegt. Wir muessen nur die zweite
# DB hinzufuegen.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    CREATE DATABASE gewerbeagent_dev OWNER ${POSTGRES_USER};
EOSQL

echo "init-databases: gewerbeagent_dev erstellt (owner=${POSTGRES_USER})"
