#!/bin/bash
# deploy_prod.sh — Deploy develop-Branch nach Produktion
#
# Workflow:
#   1. Prueft dass aktueller Branch 'main' ist
#   2. Fast-forward-Merge von origin/develop in main (kein Merge-Commit
#      → Migration-Sequenz bleibt linear)
#   3. Migration laufen lassen (alembic upgrade head)
#   4. Container restarten (Code-Reload, kein Image-Rebuild noetig wegen
#      Bind-Mount)
#   5. Tag setzen als Rollback-Anker
#
# Sicherheits-Sperre: --ff-only — wenn develop divergiert von main,
# bricht der Befehl ab (kein versehentliches Force-Push-Rollback).
#
# Rollback (nach fehlerhaftem Deploy):
#   git reset --hard prod-YYYYMMDD-HHMM
#   docker compose -p prod -f docker-compose.prod.yml restart framework
#
# Verwendung:
#   cd /opt/gewerbeagent/framework
#   ./scripts/deploy_prod.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# Sicherheits-Check 1: muss in /opt/gewerbeagent/framework laufen
if [[ "$(pwd)" != "/opt/gewerbeagent/framework" ]]; then
    echo "FEHLER: Skript muss aus /opt/gewerbeagent/framework laufen"
    echo "Aktuell: $(pwd)"
    exit 1
fi

# Sicherheits-Check 2: muss main-Branch sein
current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "main" ]]; then
    echo "FEHLER: Aktueller Branch ist '$current_branch', muss 'main' sein"
    echo "Wechsel mit: git checkout main"
    exit 1
fi

# Sicherheits-Check 3: keine uncommittedte Aenderungen
if ! git diff-index --quiet HEAD --; then
    echo "FEHLER: Es gibt uncommittedte Aenderungen. Bitte erst committen oder stashen."
    git status --short
    exit 1
fi

echo "==> Fetch origin..."
git fetch origin

echo "==> Vorher: $(git rev-parse --short HEAD) ($(git log -1 --format='%s'))"

echo "==> Fast-forward-Merge von origin/develop..."
git merge --ff-only origin/develop

echo "==> Nachher: $(git rev-parse --short HEAD) ($(git log -1 --format='%s'))"

# Rollback-Tag setzen (mit Zeitstempel)
tag="prod-$(date +%Y%m%d-%H%M)"
git tag "$tag"
echo "==> Tag gesetzt: $tag"

echo "==> Migrations ausfuehren..."
docker compose -p prod -f docker-compose.prod.yml exec -T framework \
    uv run alembic upgrade head

echo "==> Container restart (Code-Reload)..."
docker compose -p prod -f docker-compose.prod.yml restart framework

echo "==> Warte 10s auf Service-Start..."
sleep 10

echo "==> Health-Check..."
if docker compose -p prod -f docker-compose.prod.yml exec -T framework \
        curl -sf http://localhost:8001/ > /dev/null; then
    echo "==> ✅ Prod deployed: $(git rev-parse --short HEAD) [$tag]"
else
    echo "==> ⚠️  Health-Check fehlgeschlagen — Logs pruefen:"
    echo "    docker logs gewerbeagent_framework --tail 50"
    echo "==> Rollback: git reset --hard $tag^ && docker compose -p prod restart framework"
    exit 1
fi
