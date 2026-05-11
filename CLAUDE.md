# Gewerbeagent Framework — Orientierung fuer Devs + KI-Assistenten

Multi-Tenant SaaS fuer Handwerksbetriebe. Eingehende Anfragen (Mail,
Telefon, Web-Formular) werden via Telegram-Bot + KI-Klassifikation
automatisch verarbeitet, Termine gebucht, Rechnungen geschrieben.

## Stack

- **Backend:** FastAPI + async SQLAlchemy + asyncpg, Python 3.12
- **DB:** Postgres 16 mit Alembic-Migrations
- **Deps + Run:** `uv` (nicht pip); im Container unter `/app/.venv/bin/python`
- **Reverse-Proxy:** Caddy (auto-TLS, ein Caddyfile fuer prod + status + dev)
- **Container:** Docker Compose; Prod-Stack via `docker-compose.prod.yml`
  (Project-Name `prod`), Dev via `docker-compose.dev.yml`
- **KI:** Gemini (Klassifikation, Extraktion) + Vertex AI; ElevenLabs (Voice)
- **Externe:** Microsoft Graph (Outlook + Calendar), Google (Calendar,
  Drive), Brevo (Mail), Sipgate (Telefon), Lexware (Buchhaltung)

## Code-Conventions

- **Sprache:** Code-Identifier englisch, Kommentare deutsch (Sven liest)
- **Kein Bullet-Style** in Doc-Strings, lieber fortlaufender Text
- **Schmale Helper:** lieber 5 spezifische Funktionen als ein 200-Zeiler
- **Failsafe ueberall:** Webhook-Caller, Cron-Loops, Mail-Versand —
  kein Crash darf einen Tenant blockieren. `try/except + logger.exception`
- **Strukturiertes Logging:** `logger.exception(...)` bei Exceptions,
  `logger.warning(...)` nur fuer erwartete Branches ohne Stack-Trace-
  Wert. Context via `core.logging_context.set_log_tenant(...)`.

## Migration-Regel

**Additive-Only:** keine `DROP COLUMN`, kein `ALTER COLUMN TYPE`, keine
`DROP TABLE`. Wenn ein Feld weg muss: erst `nullable=True` setzen,
backfill, dann erst loeschen (in einer Folge-Migration nach Deployment-
Stabilisierung). Historisch ist `c8790780fd31_remove_chat_id_default.py`
ein nicht-additives Pattern — Single-Outlier, nicht nachahmen.

## Wo was wohnt

```
core/api/                FastAPI-App, status_routes, anfrage_routes
core/admin/              Admin-UI (FastAPI-Templates + bcrypt-Auth)
core/database/           Engine, Session-Helper, Base
core/integrations/       brevo, microsoft, google_drive, lexware,
                         openrouteservice, cron_health, admin_alerts,
                         tenant_alert, mail_retry_cron, db_maintenance,
                         dsgvo_cleanup, failure_counter, error_tracking
core/features/           Catalog + Package-System
core/models/             SQLAlchemy-Models (alle Tabellen)
core/security/           encryption, oauth_flow, oauth_token_lookup
core/logging_context.py  contextvars-basiertes strukturiertes Logging
plugins/                 telegram_notify, kalender, mail_intake,
                         voice_init, hello (Beispiel)
migrations/versions/     Alembic
scripts/                 onboard, deploy_prod, backup_db, restore_db,
                         rotate_encryption_key, external_liveness_check,
                         assign_number, generate_qr, ...
LEGAL/                   AVV-Template, Subprozessoren-Liste
tests/                   pytest (Unit + Smoke); Integration kommt Beta-2
```

## Wichtige Dateien

- `RUNBOOK.md` — Disaster-Recovery in 9 Szenarien
- `INFRA-MANUAL-STEPS.md` — Setup-Checkliste fuer Dev-Stack +
  Backup-Cron + Admin-Bot
- `.env.prod.example` — alle env-Vars dokumentiert (Sentry, Brevo etc.)
- `Caddyfile` — drei Sites (gewerbeagent.de, status., dev.)

## Smoke-Test-Pattern

Schnellster Weg etwas im Live-Container zu testen:
```bash
docker exec gewerbeagent_framework /app/.venv/bin/python -c "
from core.models import Tenant
from core.database import AsyncSessionLocal
from sqlalchemy import select
import asyncio
async def go():
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(Tenant))).scalars().all()
        for t in rows: print(t.slug, t.status)
asyncio.run(go())
"
```

## Cron-Welt

6 Background-Tasks, gestartet in `core/api/app.py:lifespan`:

| Cron | Intervall | Zweck |
|---|---|---|
| `microsoft_cron` | 2 min | Mail-Inbox-Polling (Outlook) |
| `rechnung_payment_monitor` | 30 min | Lexware-Bezahl-Status pruefen |
| `rechnung_paid_summary` | 1/Tag 18:00 | Tages-Zusammenfassung Push |
| `dsgvo_cleanup` | 1/Tag 03:00 | Mail-Konversationen >retention loeschen |
| `mail_retry_cron` | 5 min | failed_mail_queue abarbeiten |
| `db_maintenance_cron` | 1/Tag 02:00 | audit_log, oauth_states, viz-blobs cleanup |

Heartbeats via `core.integrations.cron_health.record_heartbeat(name)`.

## Health-Endpoints

- `GET /health` (public) — Liveness
- `GET /api/status` (public) — Aggregat JSON (framework, db, cron-stati)
- `GET /status` (public) — HTML-Statuspage (Apple-Polish)
- `GET /admin/api/health` (auth) — wie `/api/status`, mehr Detail

## Deploy

Dev: `git push origin develop` → GitHub Actions `deploy-dev.yml` →
SSH + `scripts/deploy_dev.sh`.
Prod: nur via GitHub-Actions workflow_dispatch `deploy-prod.yml` mit
Confirm-String "deploy prod". Macht `git merge --ff-only origin/develop`,
`alembic upgrade head`, Container-Restart, Health-Check, Tag setzen.

## Wenn du als KI hier reinkommst

- Lies dieses File + `RUNBOOK.md` zuerst
- Tests laufen mit `docker exec gewerbeagent_framework bash -c "cd /app
  && /app/.venv/bin/python -m pytest tests/ -q"`
- Bei Plan-Mode: pruef ob ein Bestands-Modul schon existiert bevor
  neues schreiben — Doku ist verteilt
- Nicht `pip install`; immer `uv add <pkg>` + Image-Rebuild
- Nicht Webhook pro Tenant: ein zentraler Bot, ein Webhook auf `_global`
- Encrypted Felder: `OAuthToken.refresh_token`/`access_token` (property
  setter), `ToolConfig.config["encrypted_*"]` (Lexware-Keys etc.)
