# ElevenLabs einrichten: Rückruf-Tool `rueckruf_anfordern`

Schritt-für-Schritt. Das Framework ist fertig & deployed (2026-06-02) — es
fehlt nur noch diese Einrichtung **im ElevenLabs-Dashboard**. Danach nimmt
der Telefon-Assistent automatisch Rückrufbitten auf.

> Menü-Bezeichnungen können je nach ElevenLabs-Version leicht abweichen, der
> Ablauf bleibt gleich.

---

## Teil 0 — Den einen Wert besorgen, den du brauchst

Du brauchst genau **einen** geheimen Wert: das **Webhook-Secret**.

**Einfachster Weg:** Du hast bei den bestehenden Tools (z. B. `checke_kalender`)
schon einen Header `X-Webhook-Secret` gesetzt. Öffne so ein Tool im Dashboard,
**kopiere dort den Wert** dieses Headers — den brauchst du gleich für das neue
Tool. (Es ist für alle Tools derselbe Wert.)

*Alternative:* Der Wert steht auf dem Server in `.env` unter
`ELEVENLABS_WEBHOOK_SECRET`.

---

## Teil 1 — Das Tool anlegen

1. **ElevenLabs** öffnen → **Conversational AI** → deinen **Agenten** anklicken.
2. Reiter **Tools** (manchmal unter „Agent" → „Tools").
3. **Add tool** → Typ **Webhook** (heißt teils „Server tool").
4. Jetzt die folgenden Felder ausfüllen (alle Werte sind kopierbar):

**Name**
```
rueckruf_anfordern
```

**Description** (das sagt der KI, *wann* sie das Tool benutzt — wichtig!)
```
Nimmt eine Rückrufbitte auf. Rufe dieses Tool auf, wenn der Anrufer
ausdrücklich mit einem Menschen oder Mitarbeiter sprechen möchte, verärgert
ist, ODER ein Anliegen hat, das du nicht selbst erledigen kannst (z. B.
Beschwerde, individuelle Preisfrage, Sonderfall). Es gibt keine
Live-Weiterleitung. Erfasse stattdessen Telefonnummer und Anliegen (Name
wenn möglich), damit der Betrieb zurückruft.
```

**Method**
```
POST
```

**URL**
```
https://gewerbeagent.de/webhook/_global/voice_init/rueckruf_anfordern
```

---

## Teil 2 — Headers (zwei Stück)

Im Tool den Abschnitt **Headers** suchen → **zwei** Header hinzufügen:

| Name (Key) | Value (Wert) |
|---|---|
| `Content-Type` | `application/json` |
| `X-Webhook-Secret` | *(der in Teil 0 kopierte Secret-Wert)* |

> Ohne korrekten `X-Webhook-Secret` lehnt der Server jeden Aufruf ab (401).
> Jedes Tool braucht den Header einzeln — ElevenLabs vererbt ihn nicht.

---

## Teil 3 — Body-Parameter (fünf Stück)

Abschnitt **Body parameters** (oder „Parameters" → Typ „Body"). Lege diese
**5 Parameter** an. Für jeden: *Data type* = `String`, dazu Identifier,
Description, Required und „Value type" wie in der Tabelle.

| # | Identifier | Required | Value type | Description (in das Feld einfügen) |
|---|---|---|---|---|
| 1 | `kunde_telefon` | ✅ an | **LLM Prompt** | Telefonnummer für den Rückruf, mit Vorwahl. Aktiv erfragen, falls die Nummer unterdrückt oder unklar ist. |
| 2 | `anliegen` | ✅ an | **LLM Prompt** | Worum geht es? Kurz und konkret, damit der Betrieb vorbereitet zurückruft. |
| 3 | `kunde_name` | ⬜ aus | **LLM Prompt** | Name des Anrufers, falls genannt. |
| 4 | `kunde_email` | ⬜ aus | **LLM Prompt** | E-Mail-Adresse, falls genannt. Sonst leer lassen. |
| 5 | `tenant_slug` | ✅ an | **Dynamic Variable** | Betriebskennung. Kommt automatisch vom System — NICHT vom Anrufer erfragen. |

> ⚠️ **Der wichtigste Punkt:** Bei `tenant_slug` als „Value type" unbedingt
> **Dynamic Variable** wählen (nicht „LLM Prompt") und als Variablennamen
> `tenant_slug` eintragen. Diese Variable liefert das System beim Anruf schon
> mit (so wie bei deinen anderen Tools). Steht sie auf „LLM Prompt", rät das
> Modell den Wert → der Rückruf landet beim falschen oder keinem Betrieb.

**Tool jetzt speichern** (Save).

---

## Teil 4 — Tool dem Agenten zuweisen

In manchen ElevenLabs-Versionen muss man neu angelegte Tools dem Agenten noch
**aktiv zuweisen** (Häkchen/Toggle in der Tool-Liste des Agenten). Falls es so
einen Schalter gibt: `rueckruf_anfordern` für den Agenten aktivieren.

---

## Teil 5 — Prompt des Agenten ergänzen

Öffne den **System-Prompt** des Agenten und füge diesen Absatz hinzu
(z. B. ans Ende):

```
Du kannst Anrufe nicht live weiterleiten. Wenn der Anrufer einen Menschen oder
Mitarbeiter verlangt, verärgert ist, oder ein Anliegen hat, das du nicht selbst
erledigen kannst (Beschwerde, Sonderfall, individuelle Absprache), dann biete
einen Rückruf an: frage nach der Telefonnummer und worum es geht (Name wenn
möglich) und rufe das Tool rueckruf_anfordern auf.

Wenn das Tool Erfolg meldet, bestätige freundlich, zum Beispiel: „Ich habe Ihre
Rückrufbitte notiert – ein Mitarbeiter meldet sich schnellstmöglich bei Ihnen
unter Ihrer Nummer." Versprich keine feste Uhrzeit. Wenn das Tool einen Fehler
meldet, entschuldige dich kurz und versuche es noch einmal mit den Angaben.
```

Prompt **speichern**.

---

## Teil 6 — Testen

### a) Schnelltest ohne Anruf (empfohlen zuerst)
Auf dem Server ausführen (Secret einsetzen; `pilot` ist ein Test-Betrieb):
```bash
curl -s -X POST https://gewerbeagent.de/webhook/_global/voice_init/rueckruf_anfordern \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: DEIN_SECRET" \
  -d '{"kunde_name":"Test Anrufer","kunde_telefon":"+49 651 000","anliegen":"Test-Rückruf","tenant_slug":"pilot"}'
```
**Erwartung:** Antwort `{"success": true, ...}` UND ein Telegram-Push
„📞 Rückrufbitte" mit „✅ Erledigt"-Button. Dann im Bot `/rueckrufe` →
Eintrag erscheint und lässt sich abhaken.

### b) Echter Testanruf
Beim Betrieb anrufen und sagen „Ich möchte bitte mit einem Mitarbeiter
sprechen" bzw. ein Anliegen nennen, das die KI nicht kann. Der Agent sollte
Nummer + Anliegen erfragen und bestätigen, dass jemand zurückruft → Push +
`/rueckrufe`.

---

## Spickzettel (Feld → Wert)

| Feld | Wert |
|---|---|
| Tool-Name | `rueckruf_anfordern` |
| Method | `POST` |
| URL | `https://gewerbeagent.de/webhook/_global/voice_init/rueckruf_anfordern` |
| Header 1 | `Content-Type: application/json` |
| Header 2 | `X-Webhook-Secret: <Secret aus bestehendem Tool / .env>` |
| Param `kunde_telefon` | String, required, LLM Prompt |
| Param `anliegen` | String, required, LLM Prompt |
| Param `kunde_name` | String, optional, LLM Prompt |
| Param `kunde_email` | String, optional, LLM Prompt |
| Param `tenant_slug` | String, required, **Dynamic Variable** → `tenant_slug` |

### Zur Referenz: der JSON-Body, den ElevenLabs sendet
```json
{
  "kunde_name": "Frau Müller",
  "kunde_telefon": "+49 651 1234567",
  "anliegen": "Reklamation Küchenfront, Scharnier defekt",
  "kunde_email": "",
  "tenant_slug": "pilot"
}
```
Pflicht: `kunde_telefon`, `anliegen`, `tenant_slug`. Leerer `kunde_name` →
wird zu „Unbekannt". Leere `kunde_email` → wird ignoriert.
