# Kundenidentität — Bestandsaufnahme und Überarbeitungsvorschlag

**Stand:** 2026-07-14 · **Status:** Analyse, nichts geändert
**Ausgangsfrage:** Wie werden zwei gleichnamige Kunden ("Thomas Müller")
auseinandergehalten?

---

## Kurzantwort

Es wird **kein** "Thomas Müller 2" angelegt, **kein** Geburtsdatum verwendet
und **nicht** das Datum der ersten Anfrage. Die einzige Identitätsregel im
System lautet:

> **E-Mail, sonst Telefonnummer, sonst der Name.**

Und sie gilt an genau **einer** Stelle: beim Drive-Ordner.

---

## Befund 1 — Es gibt gar keinen Kunden

Es existiert **keine Kunden-Tabelle**. Ein Kunde ist im System kein Objekt,
sondern ein **Namensstring**, der auf jeder Zeile einzeln mitgeführt wird:

| Tabelle | Feld |
|---|---|
| `angebote` | `kunde_name` (+ optional `kunde_email`) |
| `rechnungen` | `kunde_name` (+ optional `kunde_email`) |
| `kundengespraeche` | `kunde_name` |
| `rueckrufe` | `kunde_name`, `kunde_telefon`, `kunde_email` |
| `anfragen` | `kunde_name`, `kunde_email`, `kunde_telefon` |
| `email_conversations` | `kunde_name`, `kunde_email` |
| `visualisierungen` | `kunde_name`, `kunde_email` |
| `tenant_kunde_drives` | `kunde_name`, `kunde_email`, `kunde_telefon`, **`kunde_key`** |

Keines dieser Felder verweist auf irgendetwas. Zwei Rechnungen an
"Thomas Müller" wissen nicht, ob sie denselben Menschen meinen.

Die **einzige Ausnahme** ist `TenantKundeDrive.kunde_key`, gebildet in
`_kunde_identity_key()` — `core/integrations/google_drive.py:352`:

```
E-Mail (lowercase)  ->  "mail:thomas@example.de"
sonst Telefon (normalisiert)  ->  "tel:+4965021234"
sonst Namens-Slug (Notnagel)  ->  "thomas-mueller"
```

---

## Befund 2 — Was konkret passiert

### Zwei Thomas Müller mit verschiedenen E-Mail-Adressen
Zwei getrennte Drive-Ordner (Keys `mail:a@…` / `mail:b@…`) — soweit korrekt.
**Aber:** Beide Ordner heißen in Drive schlicht **"Thomas Müller"**, denn der
Anzeigename ist der volle Kundenname, nicht der Schlüssel
(`google_drive.py`, `sub_name = (kunde_name or "Kunde")[:200]`).
Sven sieht zwei identisch benannte Ordner nebeneinander und kann von außen
nicht erkennen, welcher wem gehört.

### Zwei Thomas Müller ohne Mail und ohne Telefon
Der Normalfall bei einem Diktat auf der Baustelle: Der Handwerker tippt nur
den Namen. Beide fallen auf denselben Namens-Slug zurück und landen damit
**im selben Ordner**. Fotos, Notizen und Belege beider Kunden liegen
zusammen — ohne Warnung, ohne Rückfrage.

### Dieselbe Person, mal per Mail, mal per Telefon
Ergibt **zwei** Ordner (`mail:…` vs. `tel:…`). Die vorhandene
"Adoption"-Logik hängt einen alten Namens-Ordner nachträglich auf den
Identitäts-Schlüssel um, führt aber `mail:` und `tel:` **nicht** zusammen.

---

## Befund 3 — Der eigentliche Knackpunkt

Die saubere Trennung im Drive-Ordner nützt wenig, weil **überall sonst der
Name der Schlüssel ist** — auch dort, wo die Trennung zählen würde:

### Kundenprofil
`core/api/app_screens.py` → `api_kunde_profil()`
Sucht per `ILIKE` auf dem Namen über Gespräche, Angebote und Rechnungen.
→ Zwei Thomas Müller ergeben **ein Profil**, in dem die Rechnungen beider
stehen.

### Archiv (am heikelsten)
`core/integrations/google_drive.py` → `list_files_in_kunde_folder()`
Sucht den Drive-Ordner mit `kunde_name.ilike(...)` **`.limit(1)`**.
→ Bei zwei gleichnamigen Ordnern nimmt es **willkürlich einen**. Selbst wenn
die Ordner sauber getrennt angelegt wurden, zeigt die App die Dateien von
einem der beiden — nicht vorhersagbar, welchem.

### Lexware
`core/integrations/lexware.py` → `upsert_customer_contact()`
Sucht nach Namen, nimmt bei mehreren Treffern ausdrücklich den **ersten** und
loggt nur eine Warnung ("nehme ersten Match").
→ Die Rechnung des einen Müller kann am Kontakt des anderen landen.

---

## Warum das mehr ist als Unordnung

Wenn im Profil von Müller A die Rechnungsbeträge von Müller B auftauchen oder
das Archiv die Baustellenfotos des Nachbarn zeigt, ist das keine Kosmetik,
sondern eine **Vermischung personenbezogener Daten zweier Betroffener**.
Sobald so ein Profil weitergegeben oder eine Rechnung an die falsche Adresse
geschickt wird, ist es eine meldepflichtige Datenpanne.

Bei einem Handwerksbetrieb in einer Kleinstadt sind zwei "Müller" oder zwei
"Schmidt" keine Theorie, sondern der Normalfall.

---

## Vorschlag A — Echte Kunden-Tabelle (der saubere Weg)

Eine `kunden`-Tabelle mit stabiler UUID; `angebote`, `rechnungen`,
`kundengespraeche`, `rueckrufe`, `anfragen` und `tenant_kunde_drives` zeigen
per Fremdschlüssel darauf. Der Name wird zum **Anzeigefeld** statt zum
Schlüssel.

**Grober Ablauf (additive-only, wie es die Migrations-Regel verlangt):**
1. Tabelle `kunden` anlegen (id, tenant_id, name, email, telefon, adresse,
   created_at) + Unique auf (tenant_id, identity_key).
2. `kunde_id`-Spalte **nullable** auf allen betroffenen Tabellen ergänzen.
3. Backfill-Skript: bestehende Zeilen nach der heutigen Regel
   (Mail > Telefon > Name) zu Kunden zusammenziehen. **Konflikte
   protokollieren statt raten** — gleichnamige ohne Mail/Telefon sind nicht
   automatisch auflösbar und brauchen eine manuelle Entscheidung.
4. Schreibpfade auf `kunde_id` umstellen (`document_flow.py`, `voice_init`,
   `mail_pipeline`, App-Routen).
5. Lesepfade (Profil, Archiv, Kundensuche) auf `kunde_id` umstellen.
6. Erst danach, in einer Folge-Migration, die Namens-Lookups entfernen.

**Aufwand:** ordentlich. Aber die Wunde im Datenmodell heilt nicht von selbst
— sie wird mit jedem Beleg größer.

---

## Vorschlag B — Zwischenschritt (deutlich kleiner)

Den `kunde_key`, der im Drive-Kontext bereits existiert, auf den anderen
Tabellen mitführen und die drei Namens-Lookups darauf umstellen:

1. `kunde_key` (String, indiziert, nullable) auf `angebote`, `rechnungen`,
   `kundengespraeche` ergänzen; beim Schreiben aus
   `_kunde_identity_key()` befüllen.
2. `list_files_in_kunde_folder()` und `api_kunde_profil()` auf `kunde_key`
   statt `kunde_name.ilike(...)` umstellen — beseitigt das willkürliche
   `limit(1)` im Archiv.
3. In der App **nachfragen**, wenn ein Kunde ohne Mail und ohne Telefon
   angelegt wird und der Name bereits existiert ("Ist das derselbe Thomas
   Müller wie am 3. Mai, oder ein neuer?"), statt still auf den Namen
   zurückzufallen.
4. Drive-Ordner-Anzeigenamen eindeutig machen (z. B. Name + Ort oder Name +
   letzte 4 Ziffern der Telefonnummer), damit Sven sie in Drive unterscheiden
   kann.

**Deckt nicht alles ab** (Lexware bleibt namensbasiert, weil dort die
Kontakt-Identität bei Lexware liegt), entschärft aber die beiden gefährlichsten
Stellen: Profil und Archiv.

---

## Geprüfte Alternative — Identität über den Drive-Ordnernamen (verworfen)

**Idee (Henrik, 2026-07-14):** Den Drive-Ordner `"Vorname|Nachname|Email"`
nennen und damit die Identität steuern. In der Suche wird nur der Name
angezeigt — es sei denn, es gibt ihn zweimal, dann zusätzlich die E-Mail.

### Der Anzeige-Teil ist richtig und wird übernommen

"Nur den Namen zeigen, das Unterscheidungsmerkmal erst einblenden, wenn es
zweideutig wird" ist genau das gewünschte Verhalten: Die Oberfläche bleibt
sauber und stört den Handwerker nur dann, wenn es wirklich etwas zu
entscheiden gibt. **Diese Regel geht so in die Umsetzung ein** (siehe
"Übernommene Anzeigeregel" unten).

### Der Speicher-Teil wird nicht übernommen — vier Gründe

**1. Der Drive-Ordner ist die einzige Stelle, die bereits funktioniert.**
In `tenant_kunde_drives` stehen `kunde_key`, `kunde_email` und
`kunde_telefon` schon als eigene, indizierte Spalten. Der Ordnername wird zum
Wiederfinden **gar nicht benutzt** — der Code merkt sich `drive_folder_id` +
`kunde_key` in der DB und geht nie über den Namen. Strukturierte Information
noch einmal als Text in ein Namensfeld zu schreiben und danach wieder
herauszuparsen, macht sie nicht zuverlässiger.

**2. Kaputt sind die *anderen* Stellen.** Angebote, Rechnungen, Gespräche,
Kundenprofil und Lexware kennen den `kunde_key` überhaupt nicht — die arbeiten
mit dem Namensstring. Ein anderer Drive-Ordnername ändert daran nichts: Das
Profil würde weiterhin beide Müller in einen Topf werfen, Lexware weiterhin
"den ersten Treffer" nehmen. Der Umbau träfe den einen Ort, an dem das Problem
*nicht* sitzt.

**3. Drive gehört dem Tenant, nicht uns.** Sven kann seine Ordner in Drive
jederzeit umbenennen. Sobald aus `"Thomas|Müller|thomas@gmx.de"` ein
`"Müller Bad"` wird, ist der Schlüssel weg. Ein Schlüssel, den ein Dritter
frei editieren kann, ist kein Schlüssel.

**4. DSGVO:** Die E-Mail-Adresse stünde im Klartext im Ordnernamen.
Ordnernamen tauchen in Drive-Freigabedialogen, Suchergebnissen und Exporten
auf — eine Kundendaten-Kategorie würde an einer Stelle gestreut, an der sie
vorher nicht lag.

### Zwei praktische Haken kämen dazu

- **Vorname/Nachname gibt es im System nicht.** `kunde_name` ist ein einziges
  Feld. Eine Zerlegung müsste raten — und rät bei "Müller GmbH",
  "Dr. Thomas von Müller" oder "Bäckerei Schmidt & Sohn" falsch.
- **Nicht jeder Kunde hat eine E-Mail.** Wer über den Telefon-Agenten
  reinkommt, hinterlässt oft nur eine Nummer. Das Format hätte dann ein leeres
  Feld — und genau die Fälle ohne Mail sind die, in denen die Verwechslung
  überhaupt passiert.

### Übernommene Anzeigeregel

Henriks Regel, aber auf die DB gestützt statt auf den Ordnernamen:

- **Identität** bleibt in der Datenbank (`kunde_key` = Mail > Telefon > Name)
  und wird auf Angebote, Rechnungen und Gespräche ausgeweitet → das ist
  Vorschlag B.
- **Anzeige** in Kundensuche und Profil: normalerweise nur "Thomas Müller".
  Kommt der Name im Betrieb mehrfach vor, hängt die App das **kleinste
  unterscheidende Merkmal** an — E-Mail, sonst die letzten vier Ziffern der
  Telefonnummer, sonst das Datum des ersten Kontakts. Pro Trefferliste
  berechenbar, ohne irgendetwas umzubenennen.
- **Drive-Ordnername** darf einen Zusatz bekommen ("Thomas Müller
  (thomas@gmx.de)"), aber **nur als Lesehilfe** für den Menschen, der in Drive
  stöbert. Er wird nie ausgelesen — deshalb ist es auch egal, wenn Sven ihn
  umbenennt.

**In einem Satz:** Die Anzeigeregel ist richtig, sie braucht nur die Datenbank
als Grundlage statt den Ordnernamen.

---

## Sofort-Maßnahme, unabhängig von A oder B

Schritt 3 aus Vorschlag B (**Rückfrage bei Namensgleichheit ohne
Mail/Telefon**) ist der billigste wirksame Hebel: Er verhindert, dass ab
sofort **neue** Vermischungen entstehen, während die Altlast noch liegt.

---

## Offene Entscheidungen für Henrik

- [ ] Vorschlag A (Kunden-Tabelle) oder B (Zwischenschritt)?
- [ ] Wie sollen beim Backfill die **nicht auflösbaren** Fälle behandelt
      werden (gleicher Name, keine Mail, kein Telefon) — zusammenführen,
      trennen, oder zur manuellen Prüfung markieren?
- [ ] Soll der Drive-Ordnername den Lesehilfe-Zusatz bekommen? Das ändert die
      Anzeigenamen bestehender Ordner (rein kosmetisch, kein Schlüssel).
- [x] ~~Identität über den Drive-Ordnernamen steuern?~~ → verworfen, siehe
      "Geprüfte Alternative". Die **Anzeigeregel** daraus wird übernommen.
