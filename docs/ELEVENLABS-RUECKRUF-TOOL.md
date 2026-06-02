# ElevenLabs-Setup: Tool `rueckruf_anfordern`

Anleitung um das neue Rückruf-Tool im ElevenLabs-Agenten zu verdrahten.
Das Framework ist fertig (Branch `feat/rueckruf-system`) — diese Schritte
passieren **nur im ElevenLabs-Dashboard** (Prompt + Tools liegen nicht im
Repo).

> ⚠️ **Voraussetzung:** Vorher muss der Branch deployed sein
> (`alembic upgrade head` + Container-Restart), sonst existiert die Tabelle
> `rueckrufe` in der Prod-DB noch nicht und jeder Tool-Call läuft auf einen
> Fehler.

---

## 1. Neues Server-Tool (Webhook) anlegen

ElevenLabs → dein Agent → **Tools** → **Add tool** → Typ **Webhook**.

| Feld | Wert |
|---|---|
| **Name** | `rueckruf_anfordern` |
| **Description** | siehe unten (das ist die Anweisung an die KI, *wann* sie das Tool ruft) |
| **Method** | `POST` |
| **URL** | `https://gewerbeagent.de/webhook/_global/voice_init/rueckruf_anfordern` |
| **Response timeout** | 20 s (Default reicht; der Call ist schnell) |

> Die URL ist identisch aufgebaut wie bei `checke_kalender` / `speichere_kontakt`
> — gleiche Basis `…/webhook/_global/voice_init/`, nur anderer Pfad. `_global`
> ist korrekt: der echte Betrieb wird über den Parameter `tenant_slug` (unten)
> aufgelöst, nicht über die URL.

### Description (Tool-Zweck, an die KI)

```
Nimmt eine strukturierte Rückrufbitte auf. IMMER aufrufen, wenn der Anrufer
ausdrücklich mit einem Menschen/Mitarbeiter sprechen will, verärgert ist,
ODER sein Anliegen etwas ist, das du nicht selbst erledigen kannst (z. B.
Beschwerde, individuelle Preisverhandlung, Sonderfall). Es gibt KEINE
Live-Weiterleitung — du erfasst stattdessen Name, Telefonnummer und Anliegen,
damit der Betrieb zurückruft. Frage fehlende Pflichtangaben vorher nach.
```

---

## 2. Header (Authentifizierung)

Unter **Headers** zwei Einträge:

| Header | Wert |
|---|---|
| `Content-Type` | `application/json` |
| `X-Webhook-Secret` | **derselbe 64-Zeichen-Wert wie `ELEVENLABS_WEBHOOK_SECRET` in der `.env`** |

> Das ist der gleiche Secret-Wert wie bei den anderen Voice-Tools. Jedes Tool
> braucht den Header **einzeln** — ElevenLabs vererbt ihn nicht automatisch.
> Ohne korrekten Header antwortet das Framework in Produktion mit `401`
> (fail-closed).

---

## 3. Body-Parameter

Vier Parameter, die die KI aus dem Gespräch füllt, plus `tenant_slug` aus
einer Dynamic Variable.

| Identifier | Typ | Required | Value type | Beschreibung (an die KI) |
|---|---|---|---|---|
| `kunde_telefon` | String | ✅ ja | LLM Prompt | Telefonnummer für den Rückruf. Mit Vorwahl. Aktiv erfragen, falls die Anrufer-Nummer unterdrückt/unklar ist. |
| `anliegen` | String | ✅ ja | LLM Prompt | Worum geht es? Kurz und konkret, damit der Betrieb vorbereitet zurückruft. |
| `kunde_name` | String | ⬜ nein | LLM Prompt | Name des Anrufers. |
| `kunde_email` | String | ⬜ nein | LLM Prompt | E-Mail, falls genannt. Sonst leer lassen. |
| `tenant_slug` | String | ✅ ja | **Dynamic Variable** → `tenant_slug` | Betriebskennung. Kommt aus der Conversation-Initiation, NICHT von der KI erfragen. |

> `tenant_slug` muss auf **Dynamic Variable** stehen (nicht „LLM Prompt"),
> Variablenname `tenant_slug` — genau die Variable, die der Initiation-Webhook
> schon liefert (wie bei den anderen Tools). Wird sie als LLM-Feld gelassen,
> rät das Modell den Slug → Rückruf landet beim falschen/keinem Betrieb.

### Request-Body (so sieht der POST aus, den ElevenLabs sendet)

```json
{
  "kunde_name": "Frau Müller",
  "kunde_telefon": "+49 651 1234567",
  "anliegen": "Reklamation Küchenfront, Scharnier defekt",
  "kunde_email": "",
  "tenant_slug": "pilot"
}
```

Pflicht sind `kunde_telefon`, `anliegen`, `tenant_slug`. `kunde_name` leer →
das Framework speichert „Unbekannt". `kunde_email` leer → wird ignoriert.

### Antwort (so reagiert das Framework)

Erfolg:
```json
{
  "success": true,
  "rueckruf_id": "…uuid…",
  "status": "offen",
  "message": "Rueckrufbitte erfasst",
  "routing": { "...": "..." }
}
```
Fehler (z. B. Pflichtfeld fehlt / Betrieb unbekannt):
```json
{ "success": false, "error": "kunde_telefon und anliegen sind Pflicht" }
```

---

## 4. Agent-Prompt ergänzen

In den System-Prompt des Agenten aufnehmen (sinngemäß):

```
Du kannst keine Anrufe live weiterleiten. Wenn der Anrufer einen Menschen /
Mitarbeiter verlangt, verärgert ist, oder ein Anliegen hat, das du nicht
selbst erledigen kannst (Beschwerde, Sonderfall, individuelle Absprache),
dann biete einen Rückruf an: erfrage Telefonnummer und worum es geht (Name
wenn möglich) und rufe das Tool `rueckruf_anfordern` auf.

Nach erfolgreichem Tool-Call ("success": true): bestätige freundlich, z. B.
„Ich habe Ihre Rückrufbitte notiert — ein Mitarbeiter meldet sich
schnellstmöglich bei Ihnen unter <Nummer>." Verspreche KEINE feste Uhrzeit.
Bei "success": false entschuldige dich kurz und biete an, es noch einmal mit
den Angaben zu versuchen.
```

---

## 5. Testen (nach dem Deploy)

Schneller End-to-End-Test ohne echten Anruf — POST direkt ans Webhook
(Secret einsetzen, `pilot` ist ein echter Test-Betrieb):

```bash
curl -s -X POST https://gewerbeagent.de/webhook/_global/voice_init/rueckruf_anfordern \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: <ELEVENLABS_WEBHOOK_SECRET>" \
  -d '{"kunde_name":"Test Anrufer","kunde_telefon":"+49 651 000","anliegen":"Test-Rückruf","tenant_slug":"pilot"}'
```

Erwartung: `{"success": true, ...}` **und** ein Telegram-Push „📞 Rückrufbitte"
beim Test-Account mit „✅ Erledigt"-Button. Danach `/rueckrufe` im Bot →
Eintrag taucht auf und lässt sich abhaken. (Test-Daten danach ggf. wieder
abhaken/entfernen.)
