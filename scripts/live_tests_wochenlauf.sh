#!/bin/bash
# Woechentlicher Lauf der Tests gegen ECHTE Anbieter (pytest -m slow).
#
# Warum es das braucht: `addopts = -m 'not slow'` nimmt diese Tests aus
# dem normalen Lauf heraus. Beim Audit am 2026-08-23 stellte sich heraus,
# dass sie seit Mai rot waren, ohne dass es jemand sah — sie pruefen als
# einzige, ob Q gegen das echte Modell noch die richtigen Entscheidungen
# trifft. Genau die Klasse Fehler, die die gemockte Suite nie findet.
#
# Schlaegt der Lauf fehl, geht ein Alarm ueber den regulaeren Weg raus
# (Mail ueber ein fremdes Postfach, sonst Web-Push).
#
# Crontab (Host):  0 6 * * 1  /opt/gewerbeagent/framework/scripts/live_tests_wochenlauf.sh >> /var/log/gewerbeagent-livetests.log 2>&1
set -uo pipefail

cd /opt/gewerbeagent/framework || exit 1
echo "=== Live-Testlauf $(date -Is) ==="

AUSGABE=$(docker compose exec -T -e PYTHONPATH=/app framework \
    .venv/bin/python -m pytest -q -m slow 2>&1)
CODE=$?
echo "$AUSGABE" | tail -20

if [ $CODE -ne 0 ]; then
    ZUSAMMENFASSUNG=$(echo "$AUSGABE" | grep -E "^(FAILED|ERROR)|failed" | head -10)
    docker compose exec -T -e PYTHONPATH=/app framework .venv/bin/python -c "
import asyncio, sys
from core.integrations.admin_alerts import notify_sven_admin_alert
text = sys.stdin.read()
asyncio.run(notify_sven_admin_alert(
    kind='live_tests_rot',
    message='Woechentlicher Live-Testlauf gegen echte Anbieter ist rot:\n' + text,
    details={'quelle': 'live_tests_wochenlauf.sh'},
))
" <<< "$ZUSAMMENFASSUNG"
fi
exit $CODE
