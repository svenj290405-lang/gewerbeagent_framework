# Eigenen Telegram-Bot einrichten

**In 5 Minuten zu deinem eigenen Assistenten-Bot — mit deinem Namen.**

Standardmäßig läuft alles über unseren gemeinsamen Bot. Das funktioniert
einwandfrei. Wenn du möchtest, kannst du stattdessen einen **eigenen Bot**
mit **deinem eigenen Namen** verwenden (z.B. `@MeinBetrieb_Bot`). Vorteile:

- **Dein Branding** — der Bot trägt deinen Firmennamen.
- **Eigene Reserven** — du teilst dir das Tempo-Limit mit niemandem.
- **Unabhängig** — eine Störung bei anderen Betrieben betrifft dich nicht.

Das Ganze ist **optional**. Wenn du nichts machst, bleibt alles beim
gemeinsamen Bot.

---

## Was du brauchst

- Dein Handy mit **Telegram** (das hast du ja schon 🙂).
- 5 Minuten Zeit.

---

## Schritt 1 — BotFather öffnen

BotFather ist der offizielle Telegram-Dienst, mit dem man Bots anlegt.

1. Öffne in Telegram die Suche (Lupe oben) und tippe **`BotFather`** ein.
2. Wähle den Treffer mit dem **blauen Haken** ✓ (`@BotFather`).
3. Tippe unten auf **Start**.

> 👉 Direktlink: **t.me/BotFather**

---

## Schritt 2 — Neuen Bot anlegen

1. Schick BotFather die Nachricht:

   ```
   /newbot
   ```

2. **Name des Bots:** BotFather fragt nach einem Namen. Nimm deinen
   Firmennamen, z.B. `Schreinerei Müller`. (Frei wählbar, darf Leerzeichen
   enthalten.)

3. **Username:** Jetzt fragt BotFather nach einem Benutzernamen. Dieser
   **muss auf `bot` enden** und ist einmalig, z.B.:

   ```
   schreinerei_mueller_bot
   ```

   Ist der Name schon vergeben, hängt einfach eine Zahl an
   (`schreinerei_mueller_2_bot`).

---

## Schritt 3 — Token kopieren

Nach dem letzten Schritt schickt dir BotFather eine Erfolgsmeldung. Darin
steht eine **lange Zeile** — das ist dein **Token**. Sie sieht ungefähr so
aus:

```
123456789:AAE3xampleToken_abcdefghijklmnopqrstuv
```

- **Tippe lange** auf diese Zeile → **Kopieren**.
- ⚠️ Behandle den Token wie ein **Passwort**: nicht weitergeben, nicht
  öffentlich posten.

---

## Schritt 4 — Token bei uns einfügen

1. Geh zurück in den Chat mit dem **Gewerbeagent-Bot** (der, über den dein
   Onboarding lief).
2. Schick den Befehl:

   ```
   /eigenen_bot
   ```

3. Der Bot zeigt dir nochmal kurz die Schritte und wartet auf deinen Token.
4. **Füge den kopierten Token ein** (langes Tippen ins Eingabefeld →
   *Einfügen*) und sende ihn.

Wir prüfen den Token sofort, richten alles ein und sagen dir, wie dein Bot
heißt.

---

## Schritt 5 — Auf deinen Bot umsteigen

Wenn alles geklappt hat, bekommst du eine Bestätigung mit dem Namen deines
Bots und einem Link, z.B. `t.me/schreinerei_mueller_bot`.

1. **Öffne deinen eigenen Bot** über den Link.
2. Schick ihm einmal:

   ```
   /start
   ```

Ab jetzt laufen **alle** Nachrichten über deinen eigenen Bot. Fertig! 🎉

---

## Häufige Fragen

**Muss ich das machen?**
Nein. Ohne eigenen Bot läuft alles über den gemeinsamen Bot — völlig in
Ordnung.

**„Telegram lehnt diesen Token ab."**
Beim Kopieren ist vermutlich etwas verlorengegangen. Erzeuge in BotFather
mit `/token` einen neuen Token und probier `/eigenen_bot` nochmal.

**Ich möchte zurück zum gemeinsamen Bot.**
Kein Problem — melde dich kurz bei uns, wir stellen das zurück.

**Mein Bot soll ein Profilbild / eine Beschreibung haben.**
Das kannst du jederzeit selbst in BotFather setzen (`/setuserpic`,
`/setdescription`) — rein kosmetisch, nicht nötig für die Funktion.

---

*Fragen? Schreib uns: svenj05@gmx.de*
