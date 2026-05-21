# Manuelle Tests fuer die Microsoft-Mail-Pipeline

Postfach unter Test: **svenjantos@outlook.de**
Tenant: **demo** (Schreinerei Test GbR)
Poll-Intervall: alle **2 Minuten** (microsoft_cron) — also ggf. bis 2 min
warten bevor du Reaktionen siehst.

Vor jedem Test eine **frische Mail** schicken (nicht in einem alten
Thread weiterschreiben, ausser bei Test 5 "Reply-Threading").

## Wo schaue ich nach Reaktionen?

- **Posteingang von svenj05@gmx.de** — alle ausgehenden Antworten
  landen hier (Q-Reply, Bestaetigungen, Storno-Annahme, ...)
- **Status-JSON**: `curl https://<deine-domain>/api/status`
  zeigt cron-Heartbeats, Queue-Tiefe etc.
- **Container-Logs (live mitschauen)** in einem zweiten Terminal:
  ```bash
  cd /opt/gewerbeagent/framework
  docker compose logs -f framework | grep -iE "(microsoft_inbox|mail_pipeline|RELEVANT_KUNDE|intent=|bounce|push_tenant)"
  ```
- **Telegram** (Tenant-Bot) — Pushes "Neue Anfrage", "Storno",
  "Verschiebung", "Bounce", "Folge-Mail".
- **DB-Tabelle `email_conversations`** — jeder Vorgang bekommt eine
  Zeile (state, last_message_id, conversation_id_microsoft).

---

## Test 1 — Neuanfrage (RELEVANT_KUNDE + neu_anfrage)

**Schicken** von svenj05@gmx.de an **svenjantos@outlook.de**:

> Betreff: `Anfrage Kuechenmontage`
>
> Hallo,
> ich brauche eine neue Kueche eingebaut, koennt ihr mir ein
> Angebot machen? Termin moeglichst diese oder naechste Woche.
> Gruss Sven

**Erwartet (innerhalb von 2 min):**
- Antwort-Mail im GMX-Postfach mit Anrede "Hallo Sven" + Link
  zu einem Anfrage-Formular.
- Telegram-Push an Tenant: "Neue Anfrage".
- Neue Zeile in `email_conversations` mit state != CLOSED.

## Test 2 — Termin-Storno (intent=termin_stornieren)

**Schicken:**

> Betreff: `Termin am Freitag absagen`
>
> Hallo, ich muss meinen Termin am Freitag leider stornieren —
> bitte um Kenntnisnahme.

**Erwartet:**
- Storno-Bestaetigungsmail an GMX.
- Telegram-Push "Storno" an Tenant.
- Wenn ein passender Kalender-Termin existiert: in Google
  Calendar geloescht/durchgestrichen (je nach Implementation).

## Test 3 — Termin-Verschiebung (intent=termin_verschieben)

**Schicken:**

> Betreff: `Termin verschieben`
>
> Koennen wir den Termin von Donnerstag auf naechste Woche
> Montag verschieben? Vormittag waere ideal.

**Erwartet:**
- Antwort-Mail mit Slot-Vorschlaegen oder Bestaetigung.
- Telegram-Push "Verschiebung" an Tenant.

## Test 4 — Rechnungsanfrage (intent=rechnungsanfrage)

**Schicken:**

> Betreff: `Frage zur Rechnung`
>
> Ich habe eine Rueckfrage zur letzten Rechnung — wann ist die
> faellig? Habt ihr noch keine Mahnung verschickt?

**Erwartet:**
- Antwort-Mail oder Telegram-Push "Rechnungsfrage" — der genaue
  Auto-Reply-Pfad haengt davon ab, ob fuer den Kunden in der DB
  eine Rechnung existiert (mit Demo-Daten typischerweise nicht).

## Test 5 — Reply-Threading

1. Erst Test 1 ausfuehren und warten bis Q-Reply ankommt.
2. **Im GMX-Postfach** auf die Q-Reply mit "Antworten" klicken.
3. Subject NICHT aendern. Text: `Danke fuer den Link, ich fuell
   das Formular gleich aus.`

**Erwartet:**
- Pipeline erkennt die Mail als **Folge** der bestehenden
  Konversation (via Microsoft conversationId und In-Reply-To).
- KEINE neue Anfrage-Mail mit Formular-Link.
- Telegram-Push "Folge-Mail" an den zustaendigen Mitarbeiter.
- `email_conversations` bekommt KEINE neue Zeile (gleiche updaten).

## Test 6 — Bounce-Erkennung

**Schicken** an svenjantos@outlook.de **mit Betreff:**

> Betreff: `Delivery Status Notification (Failure)`
>
> This is an automated bounce message. The address you tried to
> reach could not be delivered to.

(Den Header `Auto-Submitted: auto-replied` zusaetzlich setzen wenn dein
Mail-Client das erlaubt — sonst reicht das Subject-Pattern.)

**Erwartet:**
- Mail wird **nicht** an Gemini geschickt (kein Token-Verbrauch).
- KEINE Antwort an GMX (sonst Endlosschleife).
- Log-Eintrag: `is_bounce_or_autoreply -> True`.
- Wenn die "Empfaenger nicht erreichbar"-Logik zum bouncenden
  Vorgang einen Tenant-Mitarbeiter findet: Telegram-Push
  "Bounce" mit dem betroffenen Konversations-Vorgang.

## Test 7 — NICHT_RELEVANT (Newsletter/Werbung)

**Schicken:**

> Betreff: `Newsletter Mai 2026 — neue Angebote!`
>
> Entdecken Sie unsere neuesten Produkte. Klicken Sie hier!

**Erwartet:**
- Klassifikation `NICHT_RELEVANT` oder `RELEVANT_GESCHAEFT`.
- KEINE Auto-Antwort.
- KEINE Anfrage-Konversation in der DB.

## Test 8 — Phase-2 Dialog: Slot-Vorschlaege (PROPOSE_SLOTS)

**Schicken:**

> Betreff: `Termin Werkstatt`
>
> Hallo, koennt ihr mir naechste Woche Donnerstag vormittag
> einen Termin geben? Es geht um eine kleine Reparatur an
> der Werkbank.

**Erwartet (innerhalb von 2 min):**
- Antwort-Mail mit:
  - Q's Text (z.B. "klar, hier ein paar Vorschlaege")
  - durchnummerierter Slot-Box mit 2-4 Terminvorschlaegen
    aus dem Kalender (KEIN Formular-Button)
- Telegram-Push "Termin-Slots vorgeschlagen" an Tenant
- `email_conversations.state = proposing_slots`, `proposed_slots`
  enthaelt die JSON-Liste

## Test 9 — Phase-2 Dialog: Slot-Buchung (BOOK_SLOT)

**Voraussetzung:** Test 8 vorher ausfuehren, warten bis Slots
im GMX-Postfach angekommen sind.

**Schicken** (als Reply auf die Slot-Mail):

> Hallo, der erste Termin passt mir, gerne buchen.

**Erwartet:**
- Antwort-Mail "Termin bestaetigt"-Box mit Datum + Uhrzeit
  + Anliegen (KEINE Slot-Liste mehr, KEIN Formular-Button)
- Telegram-Push "Termin gebucht (Mail-Dialog)"
- Eintrag im Google Calendar
- `email_conversations.state = booked`, `gcal_event_id` gesetzt,
  `proposed_slots = NULL`

## Test 10 — Phase-2 Dialog: Storno per Mail (CANCEL_TERMIN)

Dieser Test ueberlappt teilweise mit Test 2 (Intent-Pfad). Der
Unterschied: im Dialog-Pfad ist eine bestehende Konv im
DIALOG/PROPOSING_SLOTS-State; Q im Dialog wird CANCEL_TERMIN
direkt waehlen statt ueber den Intent-Dispatcher zu gehen.

**Voraussetzung:** mindestens ein Termin steht im Kalender fuer
deine Mail-Adresse (z.B. via Test 9 vorher).

**Schicken:**

> Hallo, ich muss meinen Termin doch leider absagen.

**Erwartet:**
- Antwort-Mail mit "Termin storniert"-Box (oder "Termin nicht
  gefunden" wenn der Kalender leer war)
- Telegram-Push "Termin storniert (Mail-Dialog)"
- `email_conversations.state = storniert`
- Kalender-Termin geloescht

## Test 11 — Voice-Bestaetigungsmail (Outbound aus Voice-Buchung)

Dieser Test ist nicht ueber Mail-Eingang ausloesbar — kommt aus dem
Voice-Flow. Wenn du den manuell triggern willst:

1. Anruf gegen die FreeSWITCH-Nummer simulieren (z.B. ueber
   `voice_init`-Webhook curl).
2. Buchung abschliessen → automatisch geht eine
   Bestaetigungsmail an die Telefonnummer-zugeordnete E-Mail-
   Adresse raus.
3. Storno-Anruf → Storno-Bestaetigungsmail.

---

## Wenn etwas nicht funktioniert

- **Keine Reaktion nach 3+ Minuten?** Check `microsoft_cron`-
  Heartbeat im Status-JSON. Cron sollte mindestens 1×/2min
  ticken.
- **Antwort kommt aber falscher Intent?** Logs nach
  `cls=... intent=... reason=...` filtern — zeigt Gemini-Output.
- **Mail klassifiziert RELEVANT_KUNDE aber Auto-Verarbeitung
  bleibt aus?** Pruefe `mail_throttle` — pro Absender gibt's ein
  Rate-Limit gegen Mail-Loops.
- **Reply-Threading scheitert?** Check ob der Reply den
  conversationId-Header von Microsoft mitbringt — in
  `email_conversations` muss die zuvor versendete Q-Reply ihre
  `last_message_id` korrekt gespeichert haben.

## Test-Mail noch mal senden

```bash
cd /opt/gewerbeagent/framework
docker compose exec -w /app -e PYTHONPATH=/app framework \
    uv run python scripts/send_test_mail.py svenj05@gmx.de
```

Das Skript schickt eine frische Pipeline-Test-Mail von
`svenjantos@outlook.de` an die angegebene Adresse (Default
`svenj05@gmx.de`).
