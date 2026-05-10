# Infra-Manual-Steps: Dev/Prod-Stack-Trennung aktivieren

Phase 1 der Production-Readiness ist abgeschlossen — alle Files sind
geschrieben. Damit der Dev-Stack tatsächlich läuft, muss Sven folgende
Schritte einmalig manuell durchführen.

Ohne diese Schritte:
- Prod läuft weiter wie bisher (kein Risiko)
- Dev-Stack startet nicht (DNS / Bot-Token / Caddy-Aktivierung fehlt)

---

## Reihenfolge wichtig — Schritt für Schritt

### 1. DNS-A-Record für `dev.gewerbeagent.de` (Strato/Cloudflare/...)

Anlegen:
```
Typ:  A
Name: dev
Wert: <Server-IP von gewerbeagent.de>
TTL:  300
```

Verifizieren:
```bash
dig +short dev.gewerbeagent.de
# muss die Server-IP liefern
```

**Bevor das nicht stimmt: Caddy holt kein TLS-Cert → Dev-Stack ist nicht
über HTTPS erreichbar.**

---

### 2. Dev-Telegram-Bot anlegen

In Telegram bei [@BotFather](https://t.me/BotFather):
```
/newbot
Name:     Q Dev (Gewerbeagent Dev)
Username: gewerbeagent_dev_bot   (oder ähnlich)
```

→ BotFather liefert einen Token. **Notieren** — kommt gleich in
`.env.dev`.

**Wichtig:** der Prod-Bot bleibt unverändert. Der Dev-Bot ist ein
komplett neuer Bot mit eigenem Token, eigener Webhook-URL.

---

### 3. Google OAuth Client erweitern

In der [Google Cloud Console](https://console.cloud.google.com/apis/credentials):
1. OAuth-2.0-Client-ID öffnen (der für gewerbeagent.de)
2. **Autorisierte Redirect-URIs** → ergänzen um:
   ```
   https://dev.gewerbeagent.de/oauth/callback
   ```
3. Speichern

Prod-Redirect-URI bleibt drin. Beide Stacks teilen sich denselben
OAuth-Client mit zwei Redirect-URIs.

(Optional analog für Microsoft Azure App, falls Outlook-Integration
in Dev getestet werden soll.)

---

### 4. Verzeichnis-Struktur für Dev-Stack

Auf dem Server:
```bash
cd /opt/gewerbeagent
git clone git@github.com:svenj290405-lang/gewerbeagent_framework.git framework-dev
cd framework-dev
git checkout develop

# .env.dev anlegen aus Template:
cp .env.dev.example .env.dev
nano .env.dev   # alle <PLATZHALTER> ersetzen
```

Wichtige Werte in `.env.dev`:
- `DATABASE_URL` → muss `gewerbeagent_dev` enthalten (NICHT `gewerbeagent`)
- `POSTGRES_PASSWORD` → gleicher Wert wie in Prod-`.env` (gleicher Postgres)
- `SECRET_KEY` + `ENCRYPTION_KEY` → eigene Dev-Keys generieren:
  ```bash
  openssl rand -base64 32
  ```
- `PUBLIC_URL=https://dev.gewerbeagent.de`
- Telegram-Bot-Token aus Schritt 2 (in `tool_configs.config` nach Seed)
- `DEV_CRON_DISABLED=true` (verhindert dass Dev-Cron Prod-Quoten verbraucht)

Vertex-Key kopieren (gleiches GCP-Project ist OK):
```bash
cp /opt/gewerbeagent/framework/vertex-key.json /opt/gewerbeagent/framework-dev/
cp /opt/gewerbeagent/framework/oauth_client_secret.json /opt/gewerbeagent/framework-dev/
```

---

### 5. Dev-DB anlegen

**Wenn Postgres-Volume schon existiert** (was bei dir der Fall ist —
Prod läuft seit Wochen), dann manuell:

```bash
docker compose -p prod -f docker-compose.prod.yml exec postgres \
    psql -U gewerbeagent -c "CREATE DATABASE gewerbeagent_dev OWNER gewerbeagent;"
```

Verifizieren:
```bash
docker compose -p prod -f docker-compose.prod.yml exec postgres \
    psql -U gewerbeagent -l | grep gewerbeagent
# muss beide DBs zeigen: gewerbeagent + gewerbeagent_dev
```

(Bei einem frischen Postgres-Volume erledigt das `scripts/init-databases.sh`
automatisch beim ersten Start. Bei bestehendem Volume wird der Hook
übersprungen.)

---

### 6. Prod-Stack umstellen auf docker-compose.prod.yml

**Achtung — kurze Downtime (~10s):**
```bash
cd /opt/gewerbeagent/framework
docker compose down                     # alter Stack
docker compose -p prod -f docker-compose.prod.yml up -d
```

Verifizieren:
```bash
docker compose -p prod -f docker-compose.prod.yml ps
# alle 3 Container "Up": postgres, framework, caddy
curl -s https://gewerbeagent.de/health
# {"status":"healthy"}
```

**Networks-Namen:** weil `docker-compose.prod.yml` `name: gewerbeagent_internal`
bzw. `gewerbeagent_web` setzt, kann der Dev-Stack diese Networks
referenzieren ohne Project-Prefix.

---

### 7. Dev-Stack starten

```bash
cd /opt/gewerbeagent/framework-dev
docker compose -p dev -f docker-compose.dev.yml up -d
```

Beim ersten Start läuft `alembic upgrade head` automatisch über die
leere `gewerbeagent_dev`-DB.

Logs prüfen:
```bash
docker logs gewerbeagent_framework_dev --tail 30
# "Application startup complete."
```

Demo-Tenant seeden:
```bash
docker compose -p dev -f docker-compose.dev.yml exec framework_dev \
    uv run python -m scripts.seed_dev_tenant
```

---

### 8. Caddy für Dev-Subdomain freischalten

Im `Caddyfile` ist ein Block für `dev.gewerbeagent.de` vorbereitet,
aber komplett auskommentiert.

a) Basicauth-Hash erzeugen (Schutz vor Indexierung + Bots):
```bash
docker exec gewerbeagent_caddy caddy hash-password --plaintext "DEIN-PASSWORT-HIER"
# liefert: $2a$14$abc...xyz
```

b) Caddyfile editieren:
- Block `dev.gewerbeagent.de { ... }` einkommentieren (alle `#` am
  Zeilenanfang entfernen)
- `$2a$14$REPLACE_WITH_BCRYPT_HASH_HIER` durch echten Hash ersetzen
- Speichern

c) Caddy neu laden:
```bash
docker compose -p prod -f docker-compose.prod.yml exec caddy \
    caddy reload --config /etc/caddy/Caddyfile
```

Falls Caddy mit `Error: ...` antwortet → Caddyfile-Syntax prüfen (z.B.
unbalanced braces). Solange noch nicht reloaded, läuft Caddy mit der
alten Config weiter — kein Prod-Risiko.

Verifizieren:
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://dev.gewerbeagent.de
# 401 (basicauth) — gut!
curl -s -u sven:DEIN-PASSWORT-HIER https://dev.gewerbeagent.de/health
# {"status":"healthy"}
```

---

### 9. Dev-Bot mit Webhook konfigurieren

In dev-DB den Bot-Token als ToolConfig für den globalen Tenant setzen:
```bash
docker compose -p dev -f docker-compose.dev.yml exec framework_dev \
    uv run python -c "
import asyncio
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Tenant, ToolConfig

async def setup():
    async with AsyncSessionLocal() as s:
        gt = (await s.execute(select(Tenant).where(Tenant.slug == 'gewerbeagent'))).scalar_one_or_none()
        if not gt:
            print('Globaler Tenant fehlt — bitte zuerst seed_dev_tenant')
            return
        tc = ToolConfig(tenant_id=gt.id, tool_name='telegram_bot', enabled=True,
                        config={'bot_token': 'DEIN-DEV-BOT-TOKEN-HIER'})
        s.add(tc)
        await s.commit()
        print('Dev-Bot-Token gespeichert')

asyncio.run(setup())
"
```

Webhook bei Telegram registrieren:
```bash
curl -s -X POST "https://api.telegram.org/bot<DEV-BOT-TOKEN>/setWebhook" \
    -d "url=https://dev.gewerbeagent.de/webhook/<global-tenant-slug>/telegram_notify/incoming" \
    -d "secret_token=<DEV_TELEGRAM_WEBHOOK_SECRET>"
```

---

### 10. End-to-End-Test

1. Telegram öffnen → Dev-Bot anschreiben (`@gewerbeagent_dev_bot`)
2. `/start sven-dev` → Bot bestätigt Verknüpfung
3. `/help` → Befehlsliste
4. `/status` → "Sven Dev-Tenant — ACTIVE"

Auf Prod parallel:
1. Prod-Bot anschreiben → läuft wie immer

→ **Trennung erfolgreich:** Sven kann jetzt auf `develop` arbeiten
(`docker exec gewerbeagent_framework_dev ...` für Dev-Migrations etc.),
ohne Kunden zu beeinflussen.

---

## Workflow ab jetzt

```
Sven entwickelt feature  → push origin develop
   ↓
./scripts/deploy_dev.sh  (auf Server, in framework-dev)
   ↓ Dev-Stack neu — Sven testet auf dev.gewerbeagent.de
   ↓ ok?
./scripts/deploy_prod.sh (auf Server, in framework)
   ↓ main fast-forward, alembic upgrade, framework restart
   ↓ Prod-Stack neu — gewerbeagent.de läuft mit dem getesteten Code
```

Rollback Prod: `git reset --hard prod-<timestamp>` und `restart framework`.

---

## Troubleshooting

**Caddy bekommt kein TLS-Cert für dev.gewerbeagent.de**
→ DNS-A-Record fehlt oder noch nicht propagiert. `dig dev.gewerbeagent.de`.

**Dev-Container kann sich nicht mit Postgres verbinden**
→ Network-Name prüfen: `docker network ls | grep gewerbeagent`. Wenn der
Prefix nicht `gewerbeagent_` ist, dann läuft Prod noch mit der alten
`docker-compose.yml`. Schritt 6 wiederholen.

**`alembic upgrade head` schlägt fehl**
→ DATABASE_URL in `.env.dev` zeigt vermutlich auf falsche DB. Sicher
dass `gewerbeagent_dev` drin steht (nicht nur `gewerbeagent`).

**Telegram-Webhook 401**
→ `secret_token` beim setWebhook-Call und `TELEGRAM_WEBHOOK_SECRET` in
`.env.dev` müssen übereinstimmen.

---

## Was ändert sich für Sven beim Programmieren?

**Bisher:**
```
Edit code → docker restart gewerbeagent_framework → Kunden sehen die Änderung
```

**Ab jetzt:**
```
Edit code in /opt/gewerbeagent/framework-dev → ./deploy_dev.sh → testen auf dev.gewerbeagent.de
                                                              ↓ ok?
                                              ./deploy_prod.sh → Kunden sehen die Änderung
```

Sven hat jetzt jederzeit einen sicheren Sandbox, in dem Drive,
Material, neue Features ausprobiert werden können ohne dass Kunden-Bots
Schaden nehmen.
