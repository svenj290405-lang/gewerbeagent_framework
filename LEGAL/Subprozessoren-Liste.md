# Subprozessoren-Liste (Stand: 11.05.2026)

Diese Liste nennt alle Dienste, an die personenbezogene Daten beim
Betrieb des Gewerbeagent-Frameworks weitergegeben werden, und ist
fester Bestandteil des Auftragsverarbeitungsvertrags zwischen
**Gewerbeagent (Sven Jantos)** und dem **Auftraggeber** (Handwerks-
betrieb / Tenant).

Der Auftraggeber wird ueber Aenderungen informiert (Mail an die
hinterlegte Kontakt-Adresse) und kann Aenderungen innerhalb von
14 Tagen widersprechen.

---

## Liste der Subprozessoren

| # | Anbieter | Zweck | Sitz | Region | Datentypen | DSGVO-Basis |
|---|---|---|---|---|---|---|
| 1 | **Hetzner Online GmbH** | Hosting (Container + Postgres + Backups) | Nuernberg, DE | EU (Falkenstein/Nuernberg) | alle Tenant-Daten + Logs | Auftragsverarbeitung (Hetzner AVV) |
| 2 | **Microsoft Ireland Operations Ltd.** | Outlook / Mail-API + Graph-Calendar | Dublin, IE | EU + USA-Backup | Mail-Inhalte, Termine, Kunden-Mail-Adressen | Standardvertragsklauseln, EU-USA Data Privacy Framework |
| 3 | **Google Ireland Ltd.** | Google Calendar, Google Drive (Kunden-Archiv), Vertex AI (Gemini) | Dublin, IE | EU + USA | Termin-Inhalte, Drive-Dateien, KI-Prompts mit Kunden-Mails | Standardvertragsklauseln, EU-USA Data Privacy Framework |
| 4 | **Sendinblue SAS (Brevo)** | Transaktionale Mails (Rechnungen, Visualisierungen) | Paris, FR | EU | Empfaenger-Mail-Adresse, Mail-Inhalt, Anhaenge | EU-intern, kein Drittlandstransfer |
| 5 | **Telegram Messenger Inc.** | Tenant-Bot (Telegram-Push) | London, UK | UK / Singapore | Telegram-User-ID, Bot-Nachrichten | UK GDPR Adequacy + Telegram-AGB |
| 6 | **Sipgate GmbH** | Voice-Telefon-Nummer + Anruf-Routing | Duesseldorf, DE | EU (DE) | Anrufer-Nummer, Anruf-Zeitstempel | Telekommunikations-Anbieter (TKG), DSGVO |
| 7 | **ElevenLabs Inc.** | Voice-AI (Telefon-Annahme-Agent) | San Francisco, USA | USA (mit SCC) | Anrufer-Audio, Transkripte | Standardvertragsklauseln (SCC), keine Trainings-Verwendung vertraglich ausgeschlossen |
| 8 | **Deepgram Inc.** | Speech-to-Text (Voice-Transkription) | San Francisco, USA | USA (mit SCC) | Audio-Snippets, Transkripte | Standardvertragsklauseln (SCC) |
| 9 | **Lexware (Haufe-Lexware GmbH & Co. KG)** | Buchhaltung (Rechnungen, Bezahl-Status) | Freiburg, DE | EU (DE) | Kunden-Stammdaten, Rechnungsbetraege, Lexware-API-Key | Auftragsverarbeitung (Lexware AVV) |

---

## Datenschutz-Links der Subprozessoren

- Hetzner: https://www.hetzner.com/de/rechtliches/datenschutz
- Microsoft: https://www.microsoft.com/de-de/trust-center/privacy/data-protection-addendum
- Google: https://workspace.google.com/intl/de/terms/dpa_terms.html
- Brevo: https://www.brevo.com/de/datenschutz/
- Telegram: https://telegram.org/privacy
- Sipgate: https://www.sipgate.de/datenschutz
- ElevenLabs: https://elevenlabs.io/privacy-policy
- Deepgram: https://deepgram.com/privacy
- Lexware: https://www.lexware.de/datenschutz/

---

## Anmerkungen

- **Speicherort:** Alle persistenten Tenant-Daten liegen in der
  Hetzner-Postgres-Datenbank in Deutschland. Subprozessoren erhalten
  nur die Daten, die fuer den jeweiligen Zweck noetig sind (z.B. die
  einzelne Mail-Empfaenger-Adresse fuer Brevo, nicht die ganze DB).
- **USA-Transfers (ElevenLabs, Deepgram):** abgedeckt durch
  Standardvertragsklauseln gemaess Art. 46 DSGVO. ElevenLabs- und
  Deepgram-Calls erfolgen nur bei aktivem Voice-Feature.
- **Training-Use-Schutz:** ElevenLabs und Deepgram haben vertraglich
  zugesichert, Kunden-Audio nicht fuer Modell-Training zu verwenden.
- **Loeschfristen:** Mail-Konversationen werden nach
  `tenant.data_retention_days` (Default 90 Tage) automatisch geloescht
  (DSGVO-Cleanup-Cron). Backups werden 90 Tage off-site aufbewahrt,
  dann ueberschrieben.

---

## Aenderungshistorie

- 2026-05-11: Erstfassung fuer Pilot-Phase.
