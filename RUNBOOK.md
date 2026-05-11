# Gewerbeagent Runbook — Disaster Recovery & Incident Response

Diese Datei ist die Quick-Reference fuer Sven (und Stellvertreter), wenn
etwas in Produktion schiefgeht. Optimiert fuer "schnell scannen, dann
handeln".

Wenn du diese Datei liest weil gerade etwas brennt: scroll direkt zum
**Quick-Diagnostics**-Block.

---

## Quick-Diagnostics — was lebt, was nicht?

```bash
# 1. Container-Status
docker ps --format '{{.Names}}\t{{.Status}}'
# erwartet: gewerbeagent_framework Up, gewerbeagent_postgres Up, gewerbeagent_caddy Up

# 2. Framework-Health
curl -s https://gewerbeagent.de/health
# erwartet: {"status":"healthy"}

# 3. DB-Liveness
docker exec gewerbeagent_postgres psql -U gewerbeagent -d gewerbeagent -c 'SELECT 1'
# erwartet: (1 row)

# 4. Cron-Heartbeat-Status
curl -s http://localhost:8001/admin/health/crons  # falls implementiert
# oder: docker logs gewerbeagent_framework --tail 100 | grep heartbeat

# 5. Letzte Logs
docker logs gewerbeagent_framework --tail 80 --since 5m
```

---

## Szenario 1 — Framework-Container ist tot

**Symptom:** Sven kriegt Telegram-Alert `⚠️ Framework antwortet nicht`,
oder `curl /health` schlaegt fehl.

**Diagnose:**
```bash
docker ps | grep framework        # Up oder Restarting?
docker logs gewerbeagent_framework --tail 200
```

**Fix-Reihenfolge:**

1. **Restart-Versuch:**
   ```bash
   docker compose -p prod -f /opt/gewerbeagent/framework/docker-compose.prod.yml \
       restart framework
   sleep 10
   curl -s https://gewerbeagent.de/health
   ```

2. **Wenn Container nicht hochkommt** (z.B. Boot-Crash wegen
   Migration-Fehler oder Code-Bug):
   ```bash
   docker logs gewerbeagent_framework --tail 200 | grep -iE 'error|crash|traceback'
   ```
   → Stack-Trace lesen. Wenn die Schuld am letzten Deploy liegt:
   ```bash
   cd /opt/gewerbeagent/framework
   git log --oneline -5
   git reset --hard prod-<vorletzter-tag>
   docker compose -p prod -f docker-compose.prod.yml restart framework
   ```

3. **DB-Probleme** (asyncpg: connection refused, Migration-Fail):
   → siehe Szenario 4.

---

## Szenario 2 — Postgres ist tot

**Symptom:** Framework-Logs zeigen
`asyncpg.exceptions.ConnectionDoesNotExistError` oder `connection refused`.

**Diagnose:**
```bash
docker ps | grep postgres
docker logs gewerbeagent_postgres --tail 100
```

**Fix-Reihenfolge:**

1. **Postgres-Restart:**
   ```bash
   docker compose -p prod -f /opt/gewerbeagent/framework/docker-compose.prod.yml \
       restart postgres
   sleep 15
   docker exec gewerbeagent_postgres pg_isready -U gewerbeagent
   ```

2. **Daten-Volume durchsehen** wenn Postgres nicht startet:
   ```bash
   docker logs gewerbeagent_postgres --tail 200
   df -h | grep volumes      # voller Disk?
   docker volume ls | grep postgres
   ```

3. **Disk voll:** alte Container-Logs / Images aufraeumen:
   ```bash
   docker system df
   docker container prune -f
   docker image prune -a -f
   # Postgres-WAL-Files koennen auch viel Platz fressen
   ```

---

## Szenario 3 — Caddy / TLS-Probleme

**Symptom:** `curl https://gewerbeagent.de` gibt SSL-Fehler oder ERR_CONNECTION_REFUSED.

**Diagnose:**
```bash
docker ps | grep caddy
docker logs gewerbeagent_caddy --tail 100
dig +short gewerbeagent.de    # noch der richtige Server?
```

**Fix:**

1. **Caddy reload:**
   ```bash
   docker compose -p prod -f /opt/gewerbeagent/framework/docker-compose.prod.yml \
       exec caddy caddy reload --config /etc/caddy/Caddyfile
   ```

2. **TLS-Cert kaputt** (z.B. Let's Encrypt Rate-Limit):
   ```bash
   docker logs gewerbeagent_caddy 2>&1 | grep -i acme
   # Wenn Rate-Limit: 7 Tage warten oder Staging-Issuer setzen
   ```

3. **DNS-Aenderung** (Server-IP-Wechsel):
   → A-Records bei Strato/Cloudflare aktualisieren. TTL 300s = 5 Min
   Propagationszeit.

---

## Szenario 4 — DB-Restore aus Backup

**Wann:** Korrupte Daten, versehentlicher DROP, Ransomware. NICHT bei
"einzelner Tenant hat Mist gemacht" — da besser per SQL gezielt fixen.

**Vorgehen (kontrollierte Downtime, ca. 15-30 Min):**

1. **Framework stoppen** damit keine neuen Writes reinkommen:
   ```bash
   docker compose -p prod -f /opt/gewerbeagent/framework/docker-compose.prod.yml \
       stop framework caddy
   ```

2. **Letztes gutes Backup auswaehlen:**
   ```bash
   ls -lt /var/backups/gewerbeagent/dump-*.sql.gz | head -5
   ```

3. **Restore in TEMP-DB:** (nicht direkt in Prod ueberschreiben!)
   ```bash
   docker exec gewerbeagent_postgres psql -U gewerbeagent \
       -c "CREATE DATABASE gewerbeagent_restore"

   /opt/gewerbeagent/framework/scripts/restore_db.sh \
       --file=/var/backups/gewerbeagent/dump-XXX.sql.gz \
       --db=gewerbeagent_restore
   ```

4. **Plausi pruefen:** Tenants, Tabellen-Counts, neueste Eintraege.
   ```bash
   docker exec gewerbeagent_postgres psql -U gewerbeagent -d gewerbeagent_restore \
       -c "SELECT slug, created_at::date FROM tenants ORDER BY slug"
   docker exec gewerbeagent_postgres psql -U gewerbeagent -d gewerbeagent_restore \
       -c "SELECT count(*) FROM rechnungen"
   ```

5. **Prod-DB umbenennen + Restore aktivieren:**
   ```bash
   docker exec gewerbeagent_postgres psql -U gewerbeagent <<SQL
     ALTER DATABASE gewerbeagent RENAME TO gewerbeagent_broken;
     ALTER DATABASE gewerbeagent_restore RENAME TO gewerbeagent;
   SQL
   ```

6. **Framework wieder hoch:**
   ```bash
   docker compose -p prod -f /opt/gewerbeagent/framework/docker-compose.prod.yml \
       start framework caddy
   curl https://gewerbeagent.de/health
   ```

7. **Smoke-Test:** Sven schickt /status im Telegram, checkt /rechnungen_anzeigen.

8. **gewerbeagent_broken liegen lassen** (mindestens 30 Tage) fuer
   Forensik oder falls noch Daten gerettet werden muessen.

---

## Szenario 5 — Encryption-Key-Rotation (B11)

**Wann:** Key-Leak vermutet, regulaere jaehrliche Rotation, oder
ENCRYPTION_KEY unter 64 Zeichen → Phase-B-Hardening.

**Vorbereitung:**

1. **Backup zuerst!**
   ```bash
   /opt/gewerbeagent/framework/scripts/backup_db.sh
   ```

2. **Neuen Key generieren:**
   ```bash
   openssl rand -base64 48      # 64 Zeichen, copy in /tmp/newkey
   ```

**Rotation:**

3. **Dry-run zum Verifizieren:**
   ```bash
   docker exec -it gewerbeagent_framework /app/.venv/bin/python \
       -m scripts.rotate_encryption_key \
       --old-key="$(grep '^ENCRYPTION_KEY=' /opt/gewerbeagent/framework/.env | cut -d= -f2-)" \
       --new-key="$(cat /tmp/newkey)"
   # erwartet: errors=0 in beiden Bloecken
   ```

4. **Framework stoppen, echt rotieren, Key tauschen, Framework starten:**
   ```bash
   docker compose -p prod -f /opt/gewerbeagent/framework/docker-compose.prod.yml \
       stop framework

   docker run --rm --network gewerbeagent_internal \
       --env-file /opt/gewerbeagent/framework/.env \
       -v /opt/gewerbeagent/framework:/app -w /app \
       <framework-image> /app/.venv/bin/python \
       -m scripts.rotate_encryption_key \
       --old-key="<OLD>" --new-key="<NEW>" --execute

   # ENCRYPTION_KEY in /opt/gewerbeagent/framework/.env austauschen
   sed -i "s|^ENCRYPTION_KEY=.*$|ENCRYPTION_KEY=$(cat /tmp/newkey)|" \
       /opt/gewerbeagent/framework/.env

   docker compose -p prod -f /opt/gewerbeagent/framework/docker-compose.prod.yml \
       start framework
   ```

5. **Smoke-Test:**
   - `/status` im Telegram (Tenant-Bots gehen?)
   - `/microsoft_status` (Microsoft-Token decrypt-bar?)
   - `/lexware_status` (Lexware-Key decrypt-bar?)

6. **Bei Problemen** (z.B. ein Token blieb mit OLD verschluesselt):
   DB-Restore aus dem Pre-Rotation-Backup (Szenario 4).

**Cleanup:**
- `/tmp/newkey` loeschen
- `gewerbeagent_broken` aus Szenario 4 nicht relevant — diese
  Rotation aendert keine DB-Struktur.

---

## Szenario 6 — Cron-Loop ist tot (z.B. Microsoft-Polling friert ein)

**Symptom:** Sven-Alert `cron_dead.<cron_name>` oder Tenant meldet
"Mails werden nicht mehr beantwortet".

**Diagnose:**
```bash
# Welcher Cron ist tot?
docker logs gewerbeagent_framework --tail 200 | grep -iE 'cron|heartbeat'

# Heartbeat-Status (in-process)
docker exec gewerbeagent_framework /app/.venv/bin/python -c "
from core.integrations.cron_health import get_health_report
import json
print(json.dumps(get_health_report(), indent=2, default=str))
"
```

**Fix:** Framework-Restart bringt alle Crons frisch hoch:
```bash
docker compose -p prod -f /opt/gewerbeagent/framework/docker-compose.prod.yml \
    restart framework
```

Wenn der Cron dauernd in einem Loop crasht: Logs zeigen den Stack-Trace,
das ist ein Code-Bug → develop fixen, deploy_dev, deploy_prod.

---

## Szenario 7 — Mail-Retry-Queue staut sich

**Symptom:** Sven-Alert `mail_retry_dead.<tenant>` oder
`failed_mail_queue` ist sehr groß.

**Diagnose:**
```bash
docker exec gewerbeagent_postgres psql -U gewerbeagent -d gewerbeagent <<SQL
  SELECT status, count(*), max(updated_at)
  FROM failed_mail_queue
  GROUP BY status;
SQL
```

**Fix:**

- **status=pending, viele:** Brevo ist down. Brevo-Status pruefen
  (status.brevo.com). Cron versucht es alle 5 min selbst.
- **status=dead, viele:** Brevo-Account suspended? API-Key abgelaufen?
  → Brevo-Dashboard pruefen, Key in `_global` `tool_configs.mail_intake`
  rotieren, danach die toten manuell re-queuen:
  ```sql
  UPDATE failed_mail_queue
  SET status='pending', attempt_count=0, next_attempt_at=now()
  WHERE status='dead' AND updated_at > now() - interval '7 days';
  ```

---

## Szenario 8 — OAuth-Token revoked (Microsoft / Google)

**Symptom:** Tenant-Push `Microsoft Outlook-Verbindung getrennt`. Cron-
Logs zeigen `invalid_grant` Fehler.

**Tenant-Aktion:** im Telegram `/microsoft_setup` bzw.
`/kalender_verbinden` erneut. Token wird neu geholt + verschluesselt
gespeichert.

**Wenn Tenant nicht reagiert:** Sven kann den `oauth_tokens`-Eintrag
manuell loeschen + den Tenant kontaktieren. Mail-Polling pausiert
automatisch (cron schluckt den Fehler).

---

## Szenario 9 — Sven ist nicht erreichbar

**Stellvertreter-Setup** (TBD — aktuell single-person ops):

Priorisierungsregel:
- **Prod ist down** UND **Sven > 2h nicht erreichbar** → Stellvertreter
  darf via SSH-Key auf den Hetzner-Server + Read-Logs / Restart-Versuch.
- **Datenverlust-Risiko** (DB-Korruption, Mass-Delete) → Stellvertreter
  hat die Befugnis, die Container zu stoppen damit kein weiterer
  Schaden entsteht.
- Stellvertreter macht **keine Code-Aenderungen** ohne Svens Freigabe.

Notwendiges Setup vor Pilot-Live:
1. Stellvertreter-SSH-Key auf Hetzner installieren
2. Diese RUNBOOK.md durchgehen mit Stellvertreter
3. Trockenuebung: 30-min-Walkthrough Szenario 1+2
4. Notfall-Kontakt-Liste an Pilot-Tenant (Dietz) geben

---

## Wichtige Datei-Pfade

```
/opt/gewerbeagent/framework/.env                   Prod-Secrets
/opt/gewerbeagent/framework/Caddyfile              Reverse-Proxy-Config
/opt/gewerbeagent/framework/docker-compose.prod.yml
/opt/gewerbeagent/framework/INFRA-MANUAL-STEPS.md  Setup-Schritte
/var/backups/gewerbeagent/                         Tagliche DB-Dumps
/var/log/gewerbeagent-backup.log                   Backup-Cron-Output
/var/log/gewerbeagent-liveness.log                 Liveness-Check-Output
```

## Wichtige Container-Namen

```
gewerbeagent_framework   FastAPI-App, Port 8001 (intern)
gewerbeagent_postgres    Postgres 16, Port 5432 (intern, persistent-Volume)
gewerbeagent_caddy       Reverse-Proxy, Port 80/443
```

## Externe Dependencies (was kann brechen wenn extern down)

| Service | Was passiert wenn down | Fallback |
|---|---|---|
| Brevo | Mails landen in failed_mail_queue, Cron retried | manuelle Mail durch Tenant |
| Microsoft Graph | Mail-Polling pausiert, neue Mails werden nicht klassifiziert | Tenant sieht's in Outlook normal |
| Google Calendar/Drive | Slot-Suche faellt aus, Drive-Archiv pausiert | Tenant nutzt Google direkt |
| ElevenLabs | Voice-Calls funktionieren nicht | Telefon klingelt durch zum Tenant |
| Sipgate | Anrufe gehen nicht raus / kommen nicht rein | Tenant nutzt sein Handy |
| Lexware | Rechnungs-Wizard pausiert, Bezahl-Polling stoppt | Tenant nutzt Lexware-UI direkt |
| Vertex AI / Gemini | Klassifikation + Extraktion pausiert | Anfragen gehen ungelesen ein |
| OpenRouteService | Smart-Routing aus | Slot-Suche ohne Fahrtzeit |

Alle Failures sind **failsafe** — ein extern-Down zerstoert nie den
Bot, blockiert nur ein Feature.

---

## Backup-Retention

- **Lokal:** 7 Tage in `/var/backups/gewerbeagent/`
- **Off-Site:** 90 Tage auf Hetzner Storage-Box (wenn `BACKUP_OFFSITE`
  in der Backup-Cron-Config gesetzt ist)
- **DSGVO-Cleanup:** Kunden-Daten werden nach Tenant-Retention-Days
  (default 90) automatisch geloescht — auch aus Backups indem alte
  Off-Site-Dumps rolliert werden

---

## Wenn alles brennt

1. **Atmen.** 99% der Faelle sind Container-Restart und damit erledigt.
2. **Wenn unsicher: Status-Quo halten** (keine destruktiven Befehle wie
   DROP, DELETE, FORCE-PUSH bevor Backup bestaetigt).
3. **Backup, Backup, Backup.** Vor jeder DB-Aenderung. Ueberlebt jeden
   Fehler.
4. **Notfall-Eskalation:** Sven (svenj290405@gmail.com).
