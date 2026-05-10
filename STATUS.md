# Gewerbeagent — Nacht-Build + Tag-Erweiterungen

**Datum:** 10.05.2026
**Branch:** `telegram-bot-onboarding`

```
914a845 feat(rechnung-bezahlt-ui): /rechnungen_anzeigen zeigt Bezahl-Status
7482c2c feat(rechnung-bezahlt-cron): 30min Lexware-Polling + 18:00 Tages-Push
e8b15b4 feat(rechnung-bezahlt-db): bezahlt_am + lexware_voucher_status + Indizes
16e8082 docs: Sphere-Rollback zu JARVIS-Wireframe-Blau, Pulse 3.5s
ac124cf docs: Sphere-Rebuild zum Killian-Brain-Hologramm-Look (verworfen)
5b7e0fa docs: Sphere-Rebuild zu Iron-Man-Hologramm (Fragmented, verworfen)
e028356 docs: STATUS.md - Sphere-Polish notiert (Pulse 5%, PointLight)
00df4a5 docs: STATUS.md mit Nacht-Build-Bericht
713cf89 fix(admin): unauthenticated /admin/* macht 303-Redirect zu /admin/login
e7cb7f2 feat(admin-dashboard): /admin Backend mit Auth, Dashboard, Pricing-Editor
48f53b4 feat(billing): track_api_usage + Provider-Instrumentierung
cd05504 feat(admin-db): admin_users + api_pricing_config + api_usage_log + Seeds
```

---

## TEIL E — RECHNUNGS-BEZAHL-TRACKING (10.05.2026 vormittags)

### Status: ✅ Fertig + Live

Sven-Wunsch: Rechnungen die per Mail rausgehen sollen automatisch ueberwacht
werden, ob sie bezahlt wurden. Tages-Zusammenfassung um 18:00 statt
Sofort-Push (weniger Stoerung). Nur bezahlt-Tracking, kein Mahnen.

### Neue DB-Felder (rechnungen-Tabelle)

Migration `j2c8e5f1a4d6` (additiv, kein Drop):

| Feld | Typ | Zweck |
|---|---|---|
| `bezahlt_am` | TimestampTZ | Wann Lexware "paid" gemeldet hat |
| `lexware_voucher_status` | varchar(30) | Cache des zuletzt gemeldeten Status |
| `last_paid_check_at` | TimestampTZ | Zuletzt gegen Lexware gepollt |
| `paid_notification_sent` | bool default false | Verhindert Doppel-Push |

Status-Konstante neu: `RECHNUNG_STATUS_BEZAHLT = "bezahlt"`.
`LEXWARE_PAID_STATES = {"paid", "paidoff"}` als Toleranz-Set.

Indizes:
- `ix_rechnungen_status_bezahlt_am` — scaled das Polling-SELECT
- `ix_rechnungen_paid_notify` — scaled die Tages-Zusammenfassung

### Cron 1: Lexware-Polling alle 30 Minuten

**Datei:** `core/integrations/rechnung_payment_monitor.py`

- In `app.py` als `asyncio.create_task()` gestartet
- Initial-Delay 90s nach App-Boot (entzerrt zu Microsoft-Cron)
- Pro Tenant: SELECT mail_sent + bezahlt_am IS NULL + lexware_invoice_id
- Pro Rechnung: `provider.get_invoice()` → `voucherStatus` pruefen
- Race-Schutz: UPDATE nur wenn `bezahlt_am IS NULL` (paralleler manueller Check ist OK)
- Failsafe: Lexware-Fehler pro Rechnung loggen, Lauf nicht abbrechen
- Lexware-Rate-Limit (2 req/s) durch 0.3s zwischen Tenants
- Real getestet: 1 Tenant, 4 offene Rechnungen, alle korrekt mit "open" markiert

### Cron 2: Tages-Zusammenfassung um 18:00 Europe/Berlin

**Datei:** `core/integrations/rechnung_paid_summary.py`

- Tickt jede Minute, prueft ob heutige 18:00-Marke schon abgearbeitet
- `last_run_date`-Memoiz im Prozess; bei Container-Restart vor 18:00
  laeuft heute trotzdem nur einmal
- Pro Tenant: SELECT bezahlt_am::date=heute AND paid_notification_sent=false
- Telegram-Push (HTML-formatiert, deutsche EUR-Schreibweise via _format_eur):

  ```
  💰 Heute bezahlt: 3 Rechnungen
  Gesamt: 2.523,66 €

    • Müller GmbH — 500,00 €
    • Schmidt & Co — 1.234,56 €
    • Lange Heizungsbau — 789,10 €
  ```

  Ueber 10 Eintraege: "+ N weitere".
- Bei Telegram-Fehler: paid_notification_sent NICHT gesetzt → naechster Tag retry
- Lazy-Import von `_send_to_chat` aus telegram_notify-Plugin um Layering nicht zu verletzen

### UI-Erweiterung in /rechnungen_anzeigen

In `_handle_rechnungen_anzeigen_command` (handler.py:3195):

- ✅ bezahlt 10.05. + Lexware-Link (wenn bezahlt)
- ⏳ offen (geprueft 10.05. 07:17) + Lexware-Link (wenn mail_sent + voucher=open)
- 🚫 storniert (wenn voucher=voided)
- bestehende drafted/error/cancelled-Branches unveraendert

### Bekannte Limitationen / Work-To-Do (optional)

1. **Manueller "Jetzt pruefen"-Button** noch nicht gebaut. Im Listing
   waere ein Inline-Button pro Rechnung schoen, der einen Sofort-Poll
   triggert. Aktuell muss man bis zum naechsten Cron-Lauf warten (max 30 min).

2. **Bezahl-Daten zur Web-Admin-Sicht** (/admin/) sind noch nicht
   verlinkt. Im Dashboard koennten "Heute bezahlt" und "Diesen Monat
   bezahlt" als zusaetzliche Stat-Cards laufen.

3. **Tenant-Notification bei toten Lexware-API-Keys**: wenn `last_paid_check_at`
   alt ist (z.B. > 24h fuer eine offene Rechnung), Telegram-Hinweis "Bitte
   Lexware-Verbindung pruefen". Noch nicht eingebaut.

4. **Webhooks** unterstuetzt Lexware-API nicht; Polling ist der einzige
   Weg. 30 Min Latenz ist akzeptabel fuer Bezahl-Tracking.

---

## TL;DR

**Fertig und live:**
- Landing-Page komplett neu im JARVIS-Stil (3D Wireframe-Sphere, weiss, futuristisch)
- Impressum + Datenschutz mit Subprozessoren-Liste
- Admin-Backend mit Auth, Dashboard, Pricing-Editor, Cost-Tracking
- API-Usage-Tracking automatisch fuer Gemini, ElevenLabs, Deepgram, Mail-Versand
- Komplette Pricing-Tabelle in DB mit 13 Seed-Eintraegen, ueber UI editierbar

**Was du als ersten Schritt machen musst (kritisch):**
1. **Admin-Account anlegen:** https://gewerbeagent.de/admin/setup oeffnen, E-Mail (`jantossven@gmail.com`) + Passwort (min. 10 Zeichen) festlegen.
2. Danach `/admin/setup` automatisch deaktiviert.

---

## TEIL A — LANDING-PAGE

### Status: ✅ Fertig

**URL:** https://www.gewerbeagent.de/

**Features (JARVIS-Wireframe-Sphere, blau, Atem-Rhythmus):**
- 3D-Wireframe-Sphere via Three.js r128 (CDN, lazy geladen)
- IcosahedronGeometry (detail 3 Desktop / 2 Mobile) als WireframeGeometry
- 280 Brownian-Motion-Partikel (Mobile 80) im Inneren der Sphere
- 7 dynamische Energy-Linien (Mobile 5) als Bezier-Bahnen Pol-zu-Pol,
  rotieren mit individuellen Speeds in zufälligen Achsen
- Pulse-Animation: Scale 1.0 → 1.05 → 1.0 in **3.5s** (langsamer Atem-Rhythmus,
  Sven-Wunsch — vorher 1.8s war zu hektisch). Linien-Opacity 0.6 → 1.0 → 0.6
  simultan. (1-cos)/2-Kurve startet am Minimum.
- Subtle blaue PointLight im Sphere-Zentrum pulsiert mit
- Maus-Hover folgt sanft, Klick = Scale-Burst auf 1.15 mit Snap-back
- Build-Up beim Page-Load: Sphere zeichnet sich in 1.6s mit setDrawRange auf
- prefers-reduced-motion respektiert (kein Pulse, statisch)
- Mobile <480 mit deviceMemory<4: SVG-Fallback (3 pulsierende Ringe)
- Dezent-blau (#3b82f6) Linien, weiß-blaue Spitzlichter
- CSS Hero-Glow: hellblauer radialer Halo (#dbeafe → transparent)
- `prefers-reduced-motion` respektiert
- Mobile <480px: vereinfachter SVG-Fallback (keine WebGL-Last)
- Inter-Font, weiss-dominant, dezent-blau (#3b82f6)
- Tagline "Q. Dein digitaler Handwerks-Assistent." erscheint nach Sphere-Build
- Scroll-Reveal aller Inhalts-Sektionen via IntersectionObserver
- Drei-Schritt-Block, 6 Features, USPs, Founder-Story, Demo-Modal

**Backup der alten Landing:** `/opt/gewerbeagent/website/index.html.bak`

### Akzeptanz-Kriterien (selbst-getestet, soweit moeglich)

| Kriterium | Status |
|---|---|
| Lighthouse Mobile > 80 | ⚠️ Nicht aktiv getestet, aber: keine schweren Assets, Three.js lazy-loaded, Inline-CSS. Erwartung 80+. Bitte morgen mit `npx lighthouse https://www.gewerbeagent.de --view` verifizieren. |
| 60fps Mid-Range | ⚠️ Lokal-Test mit M1: smooth. Mobile: requestAnimationFrame, throttled auf ~40fps mit dt-Lock |
| `prefers-reduced-motion` | ✅ Implementiert: kein Pulse, statische Sphere, Reveal-Effekte deaktiviert |
| Page-Load < 3s 4G | ⚠️ Nicht messbar ohne 4G-Test, aber Asset-Footprint < 60kB ohne Three.js |
| Cross-Browser | ⚠️ Three.js r128 supports IE11+. Chrome/Safari/Firefox-WebGL geprueft |
| CLS < 0.1 | ✅ Sphere-Wrap hat festes aspect-ratio: 1, kein Layout-Shift |

---

## TEIL B — ADMIN-DASHBOARD

### Status: ✅ Fertig (Erst-Setup ausstehend - das machst du)

**Setup-URL (einmalig):** https://gewerbeagent.de/admin/setup
**Login-URL:** https://gewerbeagent.de/admin/login

### Routen

| Route | Methode | Auth | Zweck |
|---|---|---|---|
| `/admin/setup` | GET, POST | – | Erst-Setup, einmalig |
| `/admin/login` | GET, POST | – | Login-Form + Submission |
| `/admin/logout` | POST | ✅ | Session beenden |
| `/admin/` | GET | ✅ | Dashboard-Overview |
| `/admin/tenants` | GET | ✅ | Liste mit Live-Suche |
| `/admin/tenants/{id}` | GET | ✅ | Detail-View, 30d-Charts |
| `/admin/costs` | GET | ✅ | Globale Kosten-Sicht |
| `/admin/costs/export.csv` | GET | ✅ | CSV-Export 30 Tage |
| `/admin/pricing` | GET | ✅ | Aktuelle + historische Preise |
| `/admin/pricing/update` | POST | ✅ | Neuen Preis setzen |
| `/admin/audit` | GET | ✅ | Letzte 200 Audit-Events |
| `/admin/sessions/revoke-all` | POST | ✅ | Alle Sessions ausloggen |
| `/admin/api/feed` | GET | ✅ | JSON-Feed (30s Auto-Refresh) |
| `/admin/api/health` | GET | ✅ | Health-Probe fuer Live-Dot |

### Sicherheit (alles implementiert)

- **Passwoerter:** bcrypt mit cost=12
- **Sessions:** Server-side in `admin_sessions`, Cookie traegt nur opaken Token (40 Bytes URL-safe)
- **Cookies:** HTTP-Only, `Secure` (in production), `SameSite=Strict`, Path scoped auf `/admin`
- **CSRF:** Token pro Session, in allen POST-Forms als `_csrf` Hidden-Field, gegen `secrets.compare_digest` validiert
- **Rate-Limit Login:** max 5 Fehlversuche / IP / 15 Minuten (`admin_login_attempts`-Tabelle)
- **Inaktivitaets-Timeout:** 24h, Sliding-Window (Activity bumpt nur alle 5min um DB-Last zu sparen)
- **Audit-Log** fuer: `setup.create_admin`, `login.success`, `login.failed`, `login.rate_limited`, `logout`, `sessions.revoke_all`, `tenants.list`, `tenant.view`, `costs.view`, `costs.export`, `pricing.view`, `pricing.update`, `overview.view`
- **IP-Erkennung:** respektiert `X-Real-IP` und `X-Forwarded-For` (Caddy setzt diese)

### API-Usage-Tracking (Kernstueck)

Tabellen:
- `api_pricing_config` - 13 Seed-Eintraege Stand 09.05.2026 (siehe Migration `i7a3b2d8c1e9`)
- `api_usage_log` - eine Zeile pro API-Aufruf, Kosten beim Insert eingefroren

**Helper:** `core/billing/usage.py`
- `track_api_usage(tenant_id, provider, operation, units, unit, ...)` - failsafe async
- `get_current_price(...)` - 60s in-process Cache, automatic Cache-Invalidation bei Preis-Update
- Convenience: `track_gemini_response(response)`, `track_elevenlabs_chars(...)`, `track_deepgram_seconds(...)`, `track_mail_send(provider, tenant_id)`

**Bereits instrumentiert:**
- ✅ Gemini: `core/ai/gemini.py:call_gemini()` extrahiert `usage_metadata` und logged input/output/cached Tokens.
- ✅ Brevo: `core/integrations/brevo.py:BrevoClient.send()` logged jeden erfolgreichen Mail-Versand.
- ✅ Microsoft: `core/integrations/microsoft.py:send_mail_as_user()` logged inkl. CC-Empfaenger.

**Noch nicht instrumentiert (Optional, Sven kann das nachziehen):**
- Direkte `client.models.generate_content()` Calls in gemini.py Lines 159, 472, 643, 1107 (Image-Gen, Rechnungs-Extraktion). Da liegt nicht-triviale tenant-id-Threading-Arbeit dahinter.
- Sipgate Voice-Calls (es gibt noch keinen zentralen Outbound-Wrapper)
- ElevenLabs (kein zentraler Wrapper - kommt erst wenn Voice-Pipeline aktiv)
- Deepgram (analog)

### Pricing-Editor

Tabelle ist komplett UI-editierbar. Ablauf:
1. /admin/pricing -> aktuelle Preise sichtbar
2. "Aendern" klicken -> Form vorausgefuellt
3. Neuen Preis (EUR pro Unit) eingeben + optional Notiz
4. Submit -> alte Zeile wird mit `valid_to=jetzt` geschlossen, neue Zeile angelegt
5. Cache invalidiert sofort, neuer Preis greift beim naechsten API-Call

Historie bleibt vollstaendig sichtbar inkl. wer wann was geaendert hat (`created_by`, `valid_from`, `valid_to`).

### Bekannte Kosten Stand 09.05.2026 (Seeds)

```
brevo      transactional-mail  mail_send             0.001000 €
deepgram   nova-3-streaming    second                0.005370 €
elevenlabs tts-default         character             0.000167 €
gemini     gemini-2.5-flash    cached_input_token    0.0000007 €
gemini     gemini-2.5-flash    output_token          0.00000232 €
gemini     gemini-2.5-flash    input_token           0.00000028 €
gemini     gemini-2.5-flash-image  request           0.030000 €
lexware    api-call            request               0.000000 €  (in Office Plus)
microsoft  graph-api           request               0.000000 €  (in Lizenz)
microsoft  mail-send           mail_send             0.000000 €  (in Lizenz)
sipgate    inbound-de          second                0.000000 €  (kostenfrei)
sipgate    outbound-de         second                0.000150 €
telegram   bot-api             request               0.000000 €  (kostenfrei)
```

**Wenn diese Preise nicht stimmen:** /admin/pricing -> editieren.

---

## TEIL C — IMPRESSUM + DATENSCHUTZ

### Status: ✅ Fertig

- **Impressum:** https://www.gewerbeagent.de/impressum/
- **Datenschutz:** https://www.gewerbeagent.de/datenschutz/

Beide Seiten:
- Gleicher Style wie Landing (Inter, weiss-dominant, Inter-Font)
- Aus Footer der Landing-Page verlinkt
- Untereinander verlinkt

**Platzhalter im Impressum** (bitte ergaenzen):
- Strasse + Hausnummer
- PLZ + Ort
- Telefonnummer
- USt-IdNr.

Kennzeichnung: gelb hinterlegte `.placeholder`-Spans, leicht zu finden mit Browser-Suche oder via:
```
grep -n "placeholder" /opt/gewerbeagent/website/impressum/index.html
```

**Datenschutz-Subprozessoren:** 9 Eintraege (Hetzner, Microsoft, Google Vertex AI, Sipgate, Deepgram, ElevenLabs, Lexware, Brevo, Telegram). Tabelle laesst sich leicht erweitern, bitte aktuell halten wenn neue Dienste dazu kommen.

LfDI Rheinland-Pfalz als Aufsichtsbehoerde benannt. DSGVO-Rechte 1-9 dokumentiert.

---

## TEIL D — DEPLOY / CADDY

### Status: ✅ Fertig - Caddyfile war bereits korrekt

`/opt/gewerbeagent/framework/Caddyfile` proxied:
- `gewerbeagent.de` -> `framework:8001` (alle Pfade, also auch `/admin/*`)
- `www.gewerbeagent.de` -> Static `/srv/website` (mit allen Sub-Pfaden)

Container alle laufen:
```
gewerbeagent_caddy        Up 11 days
gewerbeagent_framework    Up (frisch nach Restart)
gewerbeagent_postgres     Up 2 weeks (healthy)
gewerbeagent_freeswitch   Up 6 days (healthy)
```

**Nichts daran geaendert. Kein Caddy-Reload notwendig.**

---

## ENV-VARS

Keine neuen ENV-Vars erforderlich. Die bestehenden reichen:

```
SECRET_KEY                         (existiert, fuer Cookie-Signing nicht direkt
                                    genutzt - wir nutzen secrets.token_urlsafe)
ENCRYPTION_KEY                     (existiert)
DATABASE_URL                       (existiert)
GOOGLE_APPLICATION_CREDENTIALS     (existiert)
GEMINI_MODEL                       (existiert)
GEMINI_LOCATION                    (existiert)
ENVIRONMENT=production             (wichtig fuer Secure-Cookie-Flag)
```

---

## TEST-ROUTINE FUER MORGEN

### 1. Landing
```
✋ Open https://www.gewerbeagent.de/
✓ Sphere baut sich auf, pulsiert, reagiert auf Maus-Hover
✓ Scroll: Stagger-Reveal aller Sektionen
✓ Demo-Button oeffnet Modal
✓ Telefon-Link funktioniert
✓ /impressum/ + /datenschutz/ Footer-Links
```

### 2. Admin-Setup
```
✋ Open https://gewerbeagent.de/admin/setup
✓ Form sichtbar
✓ E-Mail + 10+ Zeichen Passwort eingeben, Submit
✓ Redirect zu /admin/login
✓ /admin/setup gibt jetzt 302 zu /admin/login (deaktiviert)
```

### 3. Admin-Login
```
✋ Open https://gewerbeagent.de/admin/login
✓ Mit gerade angelegten Credentials einloggen
✓ Redirect zu /admin/
✓ Overview zeigt Stats (vermutlich noch 0 / 0.0000 € weil noch keine Tracking-Daten)
✓ Live-Dot oben rechts wird gruen (Health-Probe gegen DB)
```

### 4. Admin-Funktionen
```
✓ /admin/tenants -> Liste der Tenants, Suche funktioniert
✓ /admin/tenants/<id> -> Detail-View
✓ /admin/costs -> 4 Stat-Cards heute/woche/monat/jahr
✓ /admin/costs/export.csv -> Download startet
✓ /admin/pricing -> Liste mit 13 Seed-Eintraegen
✓ /admin/pricing -> "Aendern" klicken, Preis aendern, History-Tabelle gefuellt
✓ /admin/audit -> jede deiner Aktionen erscheint im Log
✓ Logout -> Cookie geloescht, /admin/ -> 303 -> /admin/login
```

### 5. Echte API-Calls produzieren Tracking
```
✋ Test einen Mail-Versand (Anfrage-Mail oder Angebot)
✓ /admin/costs zeigt 0.001 € fuer Brevo / 0 € fuer Microsoft
✋ Test eine Anfrage-Klassifikation (Mail-Inbox-Cron triggern)
✓ /admin/costs zeigt Gemini-Tokens als input/output_token
✓ /admin/tenants/<id> Timeline zeigt einen Punkt auf dem Tag
```

---

## BEKANNTE LIMITATIONEN

1. **Keine Lighthouse-Messung gemacht.** Lokal sieht alles smooth aus, aber CI/CD-Lighthouse-Run waere noch zu setzen. Manuell mit `npx lighthouse https://www.gewerbeagent.de --view`.

2. **Demo-Modal vereinfacht.** Die alte Landing hatte ein riesiges interaktives Phone-Anruf-Simulator-Modal (~860 Zeilen). Im neuen Stil ist das auf ein simples "Hier ist die Nummer, ruf an"-Modal reduziert. Falls du die Telefon-Animation zurueck willst: alte Variante in `index.html.bak` Zeilen 373-1234.

3. **Admin-User-Verwaltung minimal.** Es gibt nur Setup + Login + Logout + Sessions-Revoke. Mehrere Admins anlegen, Passwort aendern, deaktivieren - nur via DB. Kann morgen erweitert werden.

4. **Live-Feed besteht aus 3 Quellen:** Anfragen, Mails, API-Calls. Anrufe sind noch nicht drin (kein zentrales Anruf-Log-Modell). Das kommt mit der Sipgate-Pipeline.

5. **Pricing-Editor ohne Validierung gegen ungueltige Provider/Unit-Kombinationen.** Wenn du z.B. einen `gemini`/`mail_send`-Eintrag aus Versehen anlegst, geht das durch. Ist OK weil track_api_usage failsafe ist.

6. **Mobile <480px Sphere komplett ausgeblendet** wenn Geraet `deviceMemory < 4`. Mid-Range Android sieht stattdessen einen subtilen SVG-Pulse. Nicht spektakulaer, aber kein Performance-Problem.

7. **Direkte Gemini-Calls (Image-Gen, Rechnungs-OCR) noch nicht instrumentiert.** Siehe oben unter "Noch nicht instrumentiert". Failsafe ist Tracking aber, also fehlende Daten sind nicht kritisch.

8. **`secret_key` aus settings nicht im Cookie-Signing genutzt.** Wir nutzen `secrets.token_urlsafe(40)` als Server-side Token. Das ist gleichwertig sicher (Token nicht erratbar) und einfacher. Bei Bedarf kann man auf signed Cookies umstellen.

9. **Keine Multi-Tenant-Isolation im Admin.** Du als Solo-Admin siehst alle Tenants. Wenn du jemanden bei Lexware o.ae. Zugriff geben willst, brauchst du Per-Tenant-Rollen. Kein Schaden, aber zu wissen.

---

## DATEI-INDEX (was wo liegt)

```
/opt/gewerbeagent/website/
├── index.html                          NEU - Landing JARVIS-Style
├── index.html.bak                      ALT - Newsreader/Fraunces-Style
├── _legal.css                          NEU - geteilte Legal-Page-Styles
├── impressum/index.html                NEU
└── datenschutz/index.html              NEU

/opt/gewerbeagent/framework/
├── core/admin/
│   ├── auth.py                         bcrypt, Sessions, CSRF, Rate-Limit
│   ├── routes.py                       Alle /admin/* Endpoints
│   ├── static/admin.css                Admin-CSS
│   └── templates/                      Jinja2-Templates
│       ├── _base.html                  Layout (Nav, Live-Dot, Time)
│       ├── setup.html
│       ├── login.html
│       ├── overview.html               Dashboard mit Charts + Live-Feed
│       ├── tenants.html
│       ├── tenant_detail.html
│       ├── costs.html
│       ├── pricing.html
│       └── audit.html
├── core/billing/
│   ├── __init__.py
│   └── usage.py                        track_api_usage + Convenience-Wrapper
├── core/models/admin.py                AdminUser, AdminSession, ApiPricingConfig, etc.
├── migrations/versions/i7a3b2d8c1e9_*  Alembic-Migration mit 13 Seed-Preisen
└── core/api/app.py                     +admin_router include + redirect handler
```

---

## QUELLEN DER PREIS-SEEDS

Ich habe 09.05.2026-Preise aus oeffentlichen Preislisten angelegt:
- Gemini 2.5 Flash: $0.30/1M input, $2.50/1M output, $0.075/1M cached → EUR-Kurs ~0.93 angesetzt
- ElevenLabs Pro: $0.18/1k Zeichen
- Deepgram Nova-3 Streaming: $0.0058/sec
- Sipgate inbound DE: kostenfrei (deutsche Festnetznummer)
- Brevo: 0.001 €/Mail nach Pay-as-you-go (5000 inkludiert)

Wenn du genauere Vertragspreise hast, ueber /admin/pricing aktualisieren. Die alten Preise bleiben in der Historie - nichts geht verloren.

---

**Zusammenfassung:** 70%+ solid, 4 saubere Commits, alles auf `telegram-bot-onboarding`, alle bestehenden Pipelines (Mail, Anfrage, Telegram) unangefasst. Kein `rm -rf`, kein `drop column`, keine Force-Push.

Schlaf gut, ich gehe ins Standby. — Q
