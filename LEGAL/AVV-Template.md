# Auftragsverarbeitungsvertrag (AVV) — Template

> **Hinweis vor Verwendung:**
> Dieses Template basiert auf dem **BfDI-Mustertext** und ist fuer den
> Einsatz mit kleinen Handwerksbetrieben gedacht (B2B-Verhaeltnis,
> Auftragswerte typ. unter €10.000 pro Jahr). Es ist **nicht
> anwaltlich gegengelesen**. Fuer groessere Auftraege oder
> branchenspezifische Anforderungen (z.B. Gesundheitswesen,
> Banken) bitte vor Vertragsabschluss einen Datenschutz-Anwalt
> hinzuziehen.

---

## Zwischen

**Auftraggeber:**
{{TENANT_COMPANY}}, vertreten durch {{TENANT_CONTACT_NAME}}
{{TENANT_STREET}}
{{TENANT_ZIP}} {{TENANT_CITY}}

(im Folgenden "Verantwortlicher")

**Auftragnehmer:**
Sven Jantos, Gewerbeagent
svenj290405@gmail.com
{{AGENT_ADDRESS}}

(im Folgenden "Auftragsverarbeiter")

wird zur Konkretisierung der datenschutzrechtlichen Verpflichtungen
folgender **Auftragsverarbeitungsvertrag** geschlossen:

---

## 1. Gegenstand und Dauer

(1) Auftragsgegenstand: Bereitstellung der Gewerbeagent-Software-as-a-
Service-Plattform fuer den Verantwortlichen zur Automatisierung von
Mail-Empfang, Termin-Buchung, Rechnungs-Erstellung und Telefon-Annahme.

(2) Vertragsdauer: bis zur Kuendigung durch eine der Parteien mit
einer Frist von 1 Monat zum Monatsende.

## 2. Konkretisierung des Auftragsinhalts

(1) **Art und Zweck:**
- Empfang und Verarbeitung eingehender Anfragen (Mail, Telefon,
  Web-Formular)
- KI-basierte Klassifikation, Termin-Vorschlag, Antwort-Generierung
- Erstellung von Rechnungen und Angeboten via Lexware-Anbindung
- Telefon-Annahme via Voice-AI

(2) **Art der personenbezogenen Daten:**
- Stammdaten der Endkunden des Verantwortlichen (Name, Anschrift,
  E-Mail, Telefon)
- Inhaltsdaten (Anfrage-Texte, Termin-Daten, Rechnungs-Daten,
  Anruf-Transkripte)
- Audio-Daten (bei aktiver Voice-Telefon-Annahme)

(3) **Kategorien betroffener Personen:**
- Privatkunden und Geschaeftskunden des Verantwortlichen
- Mitarbeiter des Verantwortlichen (Login, Telegram-Chat-ID)

## 3. Technische und organisatorische Massnahmen (TOM)

Der Auftragsverarbeiter setzt folgende Massnahmen ein:

- **Verschluesselung at-rest:** alle sensiblen Daten (OAuth-Tokens,
  API-Keys) sind in der Postgres-DB Fernet-verschluesselt.
- **Verschluesselung in-transit:** ausschliesslich HTTPS/TLS mit
  HSTS, Mindest-Version TLS 1.2.
- **Zugriffsschutz:** Admin-Zugang nur ueber bcrypt-gehashte
  Passwoerter + Session-Cookies. Login-Brute-Force-Schutz aktiv.
- **Pseudonymisierung:** Webhook-URLs verwenden zufaellig generierte
  Token statt Klartext-Identifier.
- **Backups:** taegliche Postgres-Dumps mit 7 Tagen lokaler Aufbewahrung
  und 90 Tagen Off-Site-Aufbewahrung (Hetzner Storage-Box).
- **Loeschkonzept:** Mail-Konversationen werden nach 90 Tagen (anpassbar
  pro Verantwortlichem) automatisch geloescht. Backups werden nach 90
  Tagen ueberschrieben.
- **Logging und Monitoring:** strukturiertes Audit-Log mit
  Tenant-Kontext, automatische Liveness-Pruefungen alle 5 Minuten.
- **Trennung der Mandanten:** strikte Tenant-Trennung auf DB-Ebene
  (foreign-key cascade), kein Tenant kann Daten eines anderen Tenants
  einsehen.

## 4. Berichtigung, Loeschung und Sperrung von Daten

(1) Der Auftragsverarbeiter wird bei Anfragen Betroffener (Auskunft,
Berichtigung, Loeschung, Einschraenkung) den Verantwortlichen
unverzueglich (binnen 72h) informieren und Unterstuetzung leisten.

(2) Bei Loeschungsanfragen wird die Loeschung binnen 30 Tagen
durchgefuehrt; eine Bestaetigung erfolgt schriftlich oder per Mail.

## 5. Sub-Auftragsverarbeiter (Subprozessoren)

Der Verantwortliche stimmt der Einschaltung der in
**LEGAL/Subprozessoren-Liste.md** aufgefuehrten Sub-Auftragsverarbeiter
zu (Stand 11.05.2026: 9 Anbieter, siehe separate Liste).

Aenderungen werden mindestens **14 Tage vor Wirksamwerden** per Mail
mitgeteilt. Der Verantwortliche kann widersprechen; im Streitfall steht
ihm ein ausserordentliches Kuendigungsrecht zu.

## 6. Sicherheits-Vorfaelle

Bei einer Verletzung des Schutzes personenbezogener Daten wird der
Verantwortliche unverzueglich, spaetestens binnen **24 Stunden** nach
Kenntnis des Auftragsverarbeiters, informiert. Die Meldung enthaelt
nach Moeglichkeit:
- Beschreibung der Art der Verletzung
- Kategorien und ungefaehre Anzahl der Betroffenen
- moegliche Folgen
- ergriffene oder vorgeschlagene Massnahmen

## 7. Pflichten des Verantwortlichen

(1) Der Verantwortliche bestaetigt, dass er die Rechtsgrundlage
fuer die Verarbeitung (Art. 6 DSGVO) eigenstaendig sichergestellt
hat — z.B. durch Vertrag mit seinem Kunden oder dessen Einwilligung.

(2) Der Verantwortliche stellt die Datenschutzerklaerung gegenueber
seinen Kunden auf seinen eigenen Kanaelen bereit und nennt darin den
Auftragsverarbeiter als Empfaenger der Daten.

## 8. Pflichten des Auftragsverarbeiters

(1) Der Auftragsverarbeiter verarbeitet personenbezogene Daten
ausschliesslich im Rahmen der getroffenen Vereinbarungen und nach
Weisung des Verantwortlichen.

(2) Der Auftragsverarbeiter informiert den Verantwortlichen, falls
er der Auffassung ist, dass eine Weisung gegen Datenschutzgesetze
verstoesst.

(3) Mitarbeiter des Auftragsverarbeiters werden auf das
Datengeheimnis (Art. 28 Abs. 3 lit. b DSGVO) verpflichtet.

## 9. Kontrollrechte

Der Verantwortliche hat das Recht, im Benehmen mit dem
Auftragsverarbeiter Audits durchzufuehren — entweder selbst oder
durch eine beauftragte Stelle. Kosten fuer Audits, die haeufiger als
einmal pro Jahr stattfinden, traegt der Verantwortliche.

## 10. Beendigung des Vertrages

(1) Nach Beendigung des Vertrages werden alle Daten des
Verantwortlichen auf Wunsch zurueckgegeben (Postgres-Dump) und
spaetestens 90 Tage nach Vertragsende vollstaendig geloescht.

(2) Backups werden ebenfalls nach 90 Tagen ueberschrieben.

---

## Schlussbestimmungen

Sollten einzelne Bestimmungen unwirksam sein, bleibt der Rest des
Vertrages davon unberuehrt.

Aenderungen beduerfen der Schriftform (Mail genuegt).

Gerichtsstand ist {{TENANT_CITY_OR_AGENT_CITY}}.

---

**Ort, Datum:** ____________________

**Auftraggeber (Verantwortlicher):**

Unterschrift: ____________________

**Auftragnehmer (Auftragsverarbeiter):**

Sven Jantos, Gewerbeagent

Unterschrift: ____________________
