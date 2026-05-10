#!/bin/bash
# deploy_dev.sh — Pull develop-Branch + Restart des Dev-Stacks
#
# Verwendung:
#   cd /opt/gewerbeagent/framework-dev
#   ./scripts/deploy_dev.sh
#
# Nicht ganz so streng wie deploy_prod.sh — Dev darf divergieren,
# Sven kann Branches schnell wechseln.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "$(pwd)" != "/opt/gewerbeagent/framework-dev" ]]; then
    echo "FEHLER: deploy_dev.sh muss aus /opt/gewerbeagent/framework-dev laufen"
    echo "Aktuell: $(pwd)"
    exit 1
fi

echo "==> Vorher: $(git rev-parse --short HEAD) [$(git branch --show-current)]"

echo "==> Pull..."
git pull --ff-only

echo "==> Nachher: $(git rev-parse --short HEAD)"

echo "==> Migrations..."
docker compose -p dev -f docker-compose.dev.yml exec -T framework_dev \
    uv run alembic upgrade head

echo "==> Restart..."
docker compose -p dev -f docker-compose.dev.yml restart framework_dev

echo "==> Warte 8s..."
sleep 8

if docker compose -p dev -f docker-compose.dev.yml exec -T framework_dev \
        curl -sf http://localhost:8001/ > /dev/null; then
    echo "==> ✅ Dev deployed: $(git rev-parse --short HEAD)"
else
    echo "==> ⚠️  Health-Check fehlgeschlagen:"
    echo "    docker logs gewerbeagent_framework_dev --tail 50"
    exit 1
fi
