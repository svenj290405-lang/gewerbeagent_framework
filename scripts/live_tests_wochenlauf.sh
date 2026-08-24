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
# ZWEITER VERSUCH (seit 2026-08-24): der allererste automatische Lauf am
# 2026-08-24 06:00 war rot — 8 von 10 Tests, alle mit
# `429 RESOURCE_EXHAUSTED` von Gemini. Derselbe Lauf war um 11:30 von
# Hand in 44 Sekunden gruen. Es war also nie ein Fehler im Produkt,
# sondern die Kontingentgrenze des Anbieters; zehn Modellaufrufe
# hintereinander reissen sie leicht.
#
# Ein Alarm, der aus so etwas entsteht, ist schlimmer als kein Alarm:
# nach dem zweiten Fehlalarm sieht niemand mehr hin. Deshalb wird ein
# roter Lauf einmal wiederholt und erst gemeldet, wenn er auch beim
# zweiten Mal rot ist. Kostet im Fehlerfall 10 Minuten Verzoegerung —
# bei einem woechentlichen Lauf belanglos.
#
# Crontab (Host):  0 6 * * 1  /opt/gewerbeagent/framework/scripts/live_tests_wochenlauf.sh >> /var/log/gewerbeagent-livetests.log 2>&1
set -uo pipefail

cd /opt/gewerbeagent/framework || exit 1

#: Pause vor dem zweiten Versuch — lang genug, dass ein Minutenkontingent
#: sich erholt, kurz genug fuer einen Cron-Lauf.
PAUSE_SEKUNDEN=${LIVE_TEST_PAUSE:-600}

lauf() {
    docker compose exec -T -e PYTHONPATH=/app framework \
        .venv/bin/python -m pytest -q -m slow 2>&1
}

echo "=== Live-Testlauf $(date -Is) ==="

AUSGABE=$(lauf)
CODE=$?
echo "$AUSGABE" | tail -20

if [ $CODE -ne 0 ]; then
    # Kontingentfehler beim Namen nennen, damit im Log steht, warum
    # wiederholt wurde.
    if echo "$AUSGABE" | grep -qE "RESOURCE_EXHAUSTED|429"; then
        GRUND="Kontingentgrenze des Anbieters (429)"
    else
        GRUND="rote Tests"
    fi
    echo "--- 1. Versuch rot ($GRUND) — Wiederholung in ${PAUSE_SEKUNDEN}s ---"
    sleep "$PAUSE_SEKUNDEN"

    echo "=== 2. Versuch $(date -Is) ==="
    AUSGABE=$(lauf)
    CODE=$?
    echo "$AUSGABE" | tail -20
fi

if [ $CODE -ne 0 ]; then
    ZUSAMMENFASSUNG=$(echo "$AUSGABE" | grep -E "^(FAILED|ERROR)|failed" | head -10)
    docker compose exec -T -e PYTHONPATH=/app framework .venv/bin/python -c "
import asyncio, sys
from core.integrations.admin_alerts import notify_sven_admin_alert
text = sys.stdin.read()
asyncio.run(notify_sven_admin_alert(
    kind='live_tests_rot',
    message=(
        'Woechentlicher Live-Testlauf gegen echte Anbieter ist rot — '
        'auch im zweiten Versuch:\n' + text
    ),
    details={'quelle': 'live_tests_wochenlauf.sh', 'versuche': 2},
))
" <<< "$ZUSAMMENFASSUNG"
else
    echo "--- Lauf gruen, kein Alarm ---"
fi
exit $CODE
