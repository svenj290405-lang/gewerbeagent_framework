#!/bin/bash
# deploy_prod_safe.sh — Wrapper um scripts/deploy_prod.sh, der Svens
# uncommittete Metrik-Drilldown-WIP automatisch wegpackt (stash) und nach
# dem Deploy wieder zurueckholt. So bleibt der Prod-Deploy moeglich, ohne
# die WIP committen oder von Hand stashen zu muessen.
#
# Aufruf (vom Prod-Host):
#   ! cd /opt/gewerbeagent/framework && bash scripts/deploy_prod_safe.sh
#
# Was es tut: WIP stashen -> auf main -> ./scripts/deploy_prod.sh
# (ff-merge origin/develop, alembic upgrade, Container-Restart, Health-
# Check, Rollback-Tag) -> zurueck auf develop -> WIP zurueckholen.
set -uo pipefail
cd /opt/gewerbeagent/framework

WIP_FILES="core/admin/routes.py core/admin/templates/metrics.html core/admin/templates/overview.html core/admin/templates/tenant_detail.html"
START_BRANCH="$(git branch --show-current)"
STASHED=0

restore() {
    git checkout "$START_BRANCH" >/dev/null 2>&1 || git checkout develop >/dev/null 2>&1 || true
    if [[ "$STASHED" == "1" ]]; then
        if ! git stash pop; then
            echo "WARN: 'git stash pop' fehlgeschlagen — deine WIP liegt noch im Stash."
            echo "      Mit 'git stash list' pruefen, manuell 'git stash pop'."
        fi
    fi
}
trap restore EXIT

# WIP wegpacken, falls vorhanden
if ! git diff-index --quiet HEAD -- $WIP_FILES 2>/dev/null; then
    echo "==> Metrik-WIP wird gestasht (kommt nach dem Deploy zurueck)..."
    git stash push -m "WIP Metrik-Drilldown (auto, deploy_prod_safe)" -- $WIP_FILES
    STASHED=1
fi

echo "==> Wechsle auf main..."
git checkout main

# main steht ggf. auf einem uralten Commit (aelter als deploy_prod.sh selbst).
# Erst ff-merge auf origin/develop -> bringt das Skript + neuen Code auf main.
echo "==> main per ff-merge auf origin/develop bringen..."
git fetch origin
git merge --ff-only origin/develop

echo "==> Starte Prod-Deploy..."
./scripts/deploy_prod.sh
