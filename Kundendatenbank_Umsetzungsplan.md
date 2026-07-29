# Kundendatenbank — Umsetzungsplan

**Stand:** 2026-07-17 · **Status:** Phase 1–6 umgesetzt — Migration
`e384323f5d85` live, Identity-Service (`core/services/kunde_identity.py`)
gebaut, Backfill gelaufen (18 Zeilen zugeordnet, 9 Kunden angelegt,
1 needs_review-Fall offen), alle Erstellstellen setzen `kunde_id` via
`resolve_kunde_id_safe()` (failsafe — Kundenaufloesung darf keine
Anlage blockieren), Lexware laeuft ueber gepinnte Refs. Phase 8 ist
komplett (Merge-Skript + Import); offen ist nur noch Phase 7.
**Phase-8-Nachtrag (Merge-Skript, 2026-07-17):**
`scripts/merge_kunden.py` gebaut und gegen Wegwerf-Postgres getestet
(Trockenlauf default, `--execute` scharf). Haengt die FKs aller acht
Tabellen um, verschiebt Refs, uebernimmt fehlende Merkmale, haelt
Ketten einstufig (Ziel-Kette wird vorab aufgeloest, auf die Quelle
zeigende `merged_into_id` werden mitgezogen). Abbruch bei
Ref-Konflikt im selben System, Tenant-Grenze, Zyklus; idempotent bei
Wiederholung. `needs_review` wird an der Quelle geloescht, am Ziel
nur mit `--clear-review` (Ziel kann mit einem Dritten mehrdeutig
bleiben). Doppelte Drive-Ordner am Ziel werden nur gemeldet
(kunde_key bleibt eindeutig) — Dateien manuell zusammenziehen.
**Phase-8-Nachtrag (Import, 2026-07-17):**
`scripts/import_lexware_contacts.py` gebaut (Trockenlauf default,
`--execute`, `--report-csv`, `--tenant` pflicht) und mit
FakeLexware-Provider gegen Wegwerf-Postgres getestet. Kontakte kommen
seitenweise ueber die neue `LexwareProvider.list_contacts_page()`
(rohe Dicts, Rate-Limit steckt im Provider). Zusaetzlich zum
geplanten Mapping wird auch das Telefon uebernommen (Prioritaet
business > office > mobile > private > other) — sonst faende die
Kaskade einen Bestandskunden nicht, der bisher nur angerufen hat.
Nur-Name-Kontakte laufen ueber `resolve_kunde_name_only` (rät nie);
der Ref-Existenz-Check deckt beide Konfliktrichtungen ab, ein
zweiter Lexware-Kontakt zum selben Kunden gibt needs_review +
Report statt Ref. Mehrdeutige/konfliktige Kontakte bleiben ohne Ref
und werden bei Wiederholung erneut gemeldet — Aufloesung via
merge_kunden.py, dann Import nochmal laufen lassen.
**Phase-4-Nachtrag:** Der Namens-Match im Upsert ueberspringt
Kontakte, die schon einem ANDEREN Kunden gepinnt sind (sonst kaeme
die Vermischung als „Rechnung an fremden Kontakt" zurueck), und ist
bei mitgegebenem Ort strikt ortsgleich (wie der alte Inline-Block).
Geloeschte Lexware-Kontakte werden per Ref-Repair neu aufgeloest
(`replace_contact_id`). `voice_init.save_contact` legt den Kunden
jetzt auch im eigenen Stamm an.
**Phase-5-Nachtrag:** Lesepfade laufen primaer ueber kunde_id, mit
Namens-ilike als Uebergangs-Fallback fuer Zeilen ohne kunde_id
(faellt in Phase 7 weg — nach den `Phase 5`-Markern im Code suchen).
`api_kunde_profil` nimmt zusaetzlich `?kunde_id=` fuer die praezise
Aufloesung (Vorarbeit fuer die Phase-6-Anzeigeregel; Response traegt
additiv `kunde_id`, `telefon`, `adresse`, `namensgleiche_kunden`).
Die Suche matcht auch ueber `kunden.email`.
**Phase-6-Nachtrag:** Nur-Name-Erstellpfade (Telegram-Aufnahme,
PWA-Diktat) laufen ueber `resolve_kunde_name_only()` (gemeinsame
Logik mit dem Backfill, raet nie): bei Namensgleichheit bleibt
kunde_id offen und der Nutzer bekommt die Rueckfrage — Telegram als
Inline-Buttons unter der Vorschau (callback `aufnahme:kunde:…` mit
Base64-IDs, Telegram-Limit 64 Bytes), PWA als Karte im
Diktat-Ergebnis (`kunde_frage` + `POST /gespraeche/{id}/kunde`).
„Neuer Kunde" trotz vergebenem Slug bekommt einen Suffix-Key
(`slug~rand8`) via `create_kunde_explizit_neu()`. Anzeigeregel via
`kunde_anzeige_merkmal()` (Mail > letzte 4 Tel-Ziffern >
Erstkontakt-Datum), serverseitig in der `kunden`-Liste von
`api_kunden` — Merkmal nur bei Namenskollision im Ergebnis.
**Phase-3-Nachtrag:** Die Visualisierung entsteht an beiden
Erstellstellen ohne Kundendaten — `kunde_id` wird stattdessen an den
zwei Stellen gesetzt, die `kunde_email` anhaengen (Mail-Versand,
`telegram_notify/handler.py`). Das Angebot aus einem Gespraech erbt
dessen `kunde_id` direkt statt neu aufzuloesen.
**Umsetzungs-Nachtrag:** Telefon-Stufe der Kaskade matcht per
Suffix (`phone_match_key`, letzte 8 Ziffern) statt exakt —
`normalize_phone` ergänzt bewusst keine Ländervorwahl, sonst wären
„+49 170…" und „0170…" zwei Kunden geworden (im Test aufgefallen).
**Grundlage:** [Kundenueberarbeitung_Henrik.md](Kundenueberarbeitung_Henrik.md) (Bestandsaufnahme)
**Entscheidung Henrik + Sven:** Echte Kundendatenbank statt Namensstring
(entspricht Vorschlag A), mit separater generischer Verknüpfungstabelle für
Fremdsysteme (Lexware zuerst).
**Review-Nachschärfungen 2026-07-15:** Lookup-Kaskade im Service (statt
Einzelschlüssel), zweiter Unique-Constraint auf `kunde_external_ref`,
neue Phase 8 (Onboarding-Import + Merge-Skript).
**Review-Nachschärfungen 2026-07-16 (Code-Verifikation):** Phase 4 um den
Inline-Rechnungspfad erweitert, Merge-Lookups folgen `merged_into_id`
statt zu überspringen, Phase 2 fängt Kontakt-ID-Konflikte ab. Ergänzt:
Phase 3 um die Erstellstellen der restlichen drei Tabellen, Backfill-
Regeln für Zeilen ohne Kundendaten und für Gespräche (nur Name),
Hinweis auf das tote Feld `kundengespraeche.kunde_kontakt_id`.

---

## Getroffene Weichenstellungen

| Frage | Entscheidung |
|---|---|
| Verknüpfungstabelle | **Generisch** (`kunde_external_ref`, system + external_id) — deckt Lexware jetzt ab, Drive/andere später ohne neue Tabelle |
| Backfill nicht auflösbarer Altfälle | **Markieren** (`needs_review`), zur manuellen Prüfung — nie raten |
| Kundennummer | **Nur UUID** — die technische `id` *ist* die interne Kundennummer, keine separate K-Nummer |
| Sync-Richtung Fremdsysteme | **Einmal-Import beim Onboarding** (Lexware → Gewerbeagent, Phase 8), danach führt Gewerbeagent und schreibt via `upsert_customer_contact()` zurück — kein kontinuierlicher Zwei-Wege-Sync |

---

## Datenmodell

### Neue Tabelle `kunden` — `core/models/kunde.py`

| Feld | Typ | Notiz |
|---|---|---|
| `id` | UUID PK | = interne Kundennummer |
| `tenant_id` | UUID FK → tenants (CASCADE) | |
| `name` | String(300) | Anzeigefeld, nicht mehr Schlüssel |
| `email` | String(255) nullable | |
| `telefon` | String(50) nullable | |
| `adresse` | String nullable | optional, für Lexware-Sync nützlich |
| `identity_key` | String(120) | Mail > Tel > Slug, aus `_kunde_identity_key()` |
| `needs_review` | bool default false | für nicht auflösbare Backfill-Fälle |
| `merged_into_id` | UUID FK → kunden nullable | gesetzt vom Merge-Skript (Phase 8); gemergte Kunden werden nie gelöscht (additive-only) |
| Unique | `(tenant_id, identity_key)` | |

### Neue Tabelle `kunde_external_ref` — `core/models/kunde_external_ref.py`

Generische Verknüpfung interner Kunde ↔ Fremdsystem-ID.

`id`, `tenant_id`, `kunde_id` FK, `system` (String, z. B. `"lexware"`, später
`"drive"`), `external_id` (String), `created_at`.

Zwei Unique-Constraints, beide schon in Phase 1 (solange die Tabelle leer ist):

- `(tenant_id, kunde_id, system)` — ein Kunde hat pro System höchstens eine ID.
- `(tenant_id, system, external_id)` — zwei Kunden können nie auf denselben
  Fremdkontakt zeigen. Ohne diesen Constraint wäre die Vermischung, die wir
  intern gerade beseitigen, auf Lexware-Seite wieder möglich.

Die Lexware-Kontakt-ID zieht hier ein.

### Nullable FK `kunde_id` auf Bestandstabellen

Jeweils nullable + Partial-Index `WHERE kunde_id IS NOT NULL`
(Muster wie Migration `x3za4mb6n7o9`):

`angebote` · `rechnungen` · `kundengespraeche` · `rueckrufe` ·
`anfrage_tokens` · `email_conversations` · `visualisierungen` ·
`tenant_kunde_drive`

---

## Zentraler Service — `core/services/kunde_identity.py`

Eine schmale Stelle, durch die alle Schreibpfade laufen, statt die Logik zu
streuen:

- `_kunde_identity_key()` wird aus `google_drive.py:352` hierher gezogen und
  dort re-importiert (kein Verhalten geändert).
- `resolve_or_create_kunde(session, tenant_id, name, email, telefon, *,
  ask_on_ambiguous=…) -> Kunde` — SELECT FOR UPDATE, race-safe wie der
  Drive-Helper heute schon (`get_or_create_kunde_folder`).

**Lookup-Kaskade statt Einzelschlüssel:** Der berechnete `identity_key`
allein würde den bekannten `mail:`/`tel:`-Split aus der Drive-Welt
(Bestandsaufnahme, Befund 2) in die Kundentabelle einzementieren — Kunde
ruft erst an (`tel:…`), mailt später (`mail:…`), Lookup findet nichts,
Duplikat entsteht. Deshalb matcht der Service in dieser Reihenfolge:

1. exakter `identity_key`
2. `email`-Spalte (getrimmt, lowercase)
3. `telefon`-Spalte (via `normalize_phone`, wie im Drive-Helper)

Trifft 2. oder 3., wird der bestehende Kunde genommen und das fehlende
Merkmal an ihm ergänzt. Der `identity_key` bleibt dabei unverändert — er
ist Anlage-Schlüssel, kein Lebenszeit-Schlüssel; der Unique-Constraint auf
`(tenant_id, identity_key)` bleibt bestehen. Neu angelegt wird nur, wenn
alle drei Stufen leer ausgehen.

**Gemergte Kunden: weiterverfolgen, nicht überspringen.** Trifft eine
Stufe einen Kunden mit gesetztem `merged_into_id`, folgt der Service der
Kette bis zum Merge-Ziel und liefert dieses zurück. Überspringen wäre
falsch: Kunde B behält nach dem Merge seinen `identity_key`
(additive-only, B wird nie gelöscht) — ein „übersprungener" Treffer
liefe in den Create-Zweig und knallte auf den Unique-Constraint
`(tenant_id, identity_key)`. Die Kette ist kurz (Merge-Skript setzt
`merged_into_id` immer direkt aufs finale Ziel, siehe Phase 8), die
Auflösung trotzdem mit Zyklus-Schutz.

---

## Phasen (jede ist ein eigenständiges Deploy)

### Phase 1 — Migration, additiv
Down-revision `d4f8a1c62b7e` (aktueller Head). Legt beide neuen Tabellen an,
ergänzt `kunde_id` überall nullable + Indizes. Models schreiben. Kein Code
liest/schreibt die neuen Felder → risikolos. Smoke-Test per Container-Pattern
aus CLAUDE.md.

### Phase 2 — Backfill-Skript `scripts/backfill_kunden.py`
Pro Tenant über alle Tabellen, `identity_key` bilden, Kunde finden/anlegen,
`kunde_id` setzen. Mehrdeutige Fälle (gleicher Name, keine Mail/Tel) →
`needs_review=true` + Report-Zeile, `kunde_id` bleibt NULL. **Rät nie.**
Idempotent, wiederholbar. Lexware-Kontakt-IDs aus
`rechnungen.lexware_contact_id` nach `kunde_external_ref` ziehen.

**Kontakt-ID-Konflikte: dieselbe „Rät nie"-Regel wie bei Namen.** Die
Bestandsdaten enthalten genau die Vermischung, die wir beseitigen — das
Erster-Treffer-Matching kann denselben Lexware-Kontakt an Rechnungen
zweier verschiedener Kunden gehängt haben, und ein Kunde kann über die
Zeit zwei verschiedene Kontakt-IDs bekommen haben. Beides verletzt je
einen der zwei Unique-Constraints auf `kunde_external_ref`. Das Skript
prüft deshalb vor jedem Insert beide Richtungen; bei Konflikt wird
**kein** Ref geschrieben, alle beteiligten Kunden bekommen
`needs_review=true` + Report-Zeile (Auflösung später via Merge-Skript,
Phase 8). Sonst bräche der Backfill mittendrin mit IntegrityError ab.

Typ-Detail: `rechnungen.lexware_contact_id` ist UUID,
`kunde_external_ref.external_id` ist String — einheitlich als
`str(uuid)` (lowercase, mit Bindestrichen) ablegen, sonst greift die
Idempotenz-Prüfung des Phase-8-Imports nicht.

**Zeilen ganz ohne Kundendaten:** `rechnungen.kunde_name` ist nullable —
eine Zeile ohne Name, Mail und Telefon liefert keinen `identity_key`
und damit keinen Kunden, an dem `needs_review` gesetzt werden könnte.
Für diese Fälle gibt es nur die Report-Zeile; `kunde_id` bleibt NULL.
Kein Sonderfall-Kunde („Unbekannt") — das wäre wieder Raten.

**Kundengespräche im Backfill:** nur `kunde_name` vorhanden (siehe
Phase 3) — Matching läuft ausschließlich über den Namens-Slug. Gibt es
zum Slug mehrere Kunden-Kandidaten, greift die normale
Mehrdeutigkeits-Regel (`needs_review` + Report). Hier ist die höchste
`needs_review`-Quote zu erwarten; das ist gewollt, nicht kaputt.

### Phase 3 — Schreibpfade auf `kunde_id`
An jeder Erstellstelle zusätzlich `resolve_or_create_kunde()` und `kunde_id`
setzen (alte Namensfelder bleiben befüllt — additiv):

- `document_flow.py:99` / `:221` (Angebot, Rechnung)
- `app_screens.py:1136` / `:3510` (Gespräch, Rückruf)
- `command_center.py:585`
- `voice_init/handler.py:1674`
- `telegram_notify/handler.py:4140` / `:5596` / `:5865` / `:6284`
- `anfrage_forms.py:412`

Damit sind erst 5 der 8 Tabellen abgedeckt. Die Erstellstellen der
übrigen drei gehören auch dazu — spätestens vor Phase 7 (non-null),
sonst laufen dort dauerhaft neue Zeilen mit `kunde_id = NULL` auf:

- `email_conversations` — `mail_pipeline.py:236`
- `visualisierungen` — `app_screens.py:1342` und
  `telegram_notify/handler.py:2731`
- `tenant_kunde_drive` — `google_drive.py:500`
  (in `get_or_create_kunde_folder`)

**Schwächste Identität: Kundengespräche.** `kundengespraeche` hat nur
`kunde_name`, keine Mail-/Telefon-Spalten — die Erstellstelle
(`app_screens.py:1136`) kann der Lookup-Kaskade also nur den Namen
geben, der Match läuft dort immer über den Namens-Slug. Das ist okay
(Phase 6 fängt Mehrdeutigkeit per Rückfrage ab), aber die Erwartung
gehört festgehalten: Gespräche bleiben der Pfad mit der höchsten
Duplikat-Wahrscheinlichkeit, bis die Erstellmaske Mail/Tel mitgibt.

### Phase 4 — Lexware entkoppeln
`upsert_customer_contact()` (`lexware.py:512`) bekommt optional `kunde_id`:
existiert eine `kunde_external_ref` für `"lexware"`, wird direkt diese
`contact_id` genommen — **kein namensbasiertes „erster Treffer" mehr**
(`lexware.py:547`). Neue Kontakte werden zurück in `kunde_external_ref`
geschrieben. `rechnungen.lexware_contact_id` bleibt für Kompatibilität
befüllt.

**Zweiter Schreibpfad, nicht vergessen:** Der Haupt-Rechnungsflow geht
**nicht** durch `upsert_customer_contact()` — er hat in
`telegram_notify/handler.py:7192–7231` eine eigene Inline-Kopie derselben
Logik (`search_contacts(kunde_name)` + Erster-Treffer +
`create_customer_contact`), deren Ergebnis bei `:7242` als
`lexware_contact_id` an der Rechnung landet. Das ist die Hauptquelle der
Vermischung. Der Block wird in Phase 4 auf den erweiterten Upsert
umgestellt (Inline-Logik ersetzen, nicht duplizieren) — damit läuft auch
dieser Pfad über den `kunde_external_ref`-Lookup. Einziger weiterer
Aufrufer des Upserts ist `voice_init/handler.py:1503`; die übrigen
`search_contacts`-Stellen (`handler.py:5033`, `:6661`) sind reine
Lese-/Such-UI und bleiben unberührt.

### Phase 5 — Lesepfade auf `kunde_id`
- `api_kunde_profil()` (`app_screens.py:1441`) und Kundensuche
  (`:1407`–`:1424`) über `kunde_id` statt `.ilike(name)`.
- `list_files_in_kunde_folder()` (`google_drive.py`) über `kunde_key/kunde_id`
  → beseitigt das willkürliche `.limit(1)` im Archiv.

### Phase 6 — Sofort-Maßnahme + Anzeigeregel
- Rückfrage beim Anlegen, wenn Name existiert und Mail+Tel fehlen
  („Ist das derselbe Thomas Müller wie am 3. Mai, oder ein neuer?").
- Anzeige: normal nur Name; bei Mehrdeutigkeit kleinstes unterscheidendes
  Merkmal (Mail → letzte 4 Tel-Ziffern → Erstkontakt-Datum).

### Phase 7 — später, Folge-Migration
Nach Stabilisierung in Prod (wie die Migrations-Regel verlangt): `kunde_id`
non-null setzen, Namens-Lookups entfernen. Erst wenn Phase 2–5 stabil laufen.

**Vorprüfung 2026-07-17 (ein Tag nach Livegang — noch nicht dran):**
Datenstand: nur noch 11 Zeilen ohne `kunde_id` — 10 Visualisierungen
und die Drive-Zeile `sven-jantos` (der offene needs_review-Fall,
braucht menschliche Zuordnung, kein Merge — es gibt nur einen Kunden
dieses Namens). Zwei strukturelle Erkenntnisse, die den Umfang von
Phase 7 ändern:

- **`visualisierungen` kann nie non-null werden:** Nach dem
  Phase-3-Design entstehen Visualisierungen bewusst ohne Kundendaten;
  `kunde_id` kommt erst beim Mail-Versand. Neue Zeilen laufen also
  dauerhaft mit NULL auf — die Tabelle ist vom Non-Null auszunehmen
  (der Partial-Index passt dazu schon).
- **Non-Null kollidiert mit der Failsafe-Regel:** Die Erstellstellen
  nutzen `resolve_kunde_id_safe()`, das bei fehlgeschlagener
  Aufloesung bewusst None liefert, damit keine Anlage blockiert.
  Ein hartes NOT NULL wuerde genau diese Anlagen zum Scheitern
  bringen; dazu kommen Zeilen ganz ohne Kundendaten (laut
  Backfill-Regel bewusst NULL, kein „Unbekannt"-Kunde).

**Entscheidung Henrik 2026-07-17:** so beschlossen — Phase 7 =
**Namens-Fallbacks entfernen** (die
`Phase 5`-Marker in app_screens.py + google_drive.py +
test_app_kunden_profil.py), sobald die sven-jantos-Zeile zugeordnet
ist und Phase 2–6 ein paar Wochen stabil laufen. Das harte NOT NULL
entfaellt zugunsten der Failsafe-Regel; stattdessen NULL-Quote
beobachten (neue Zeilen ausser Visualisierungen sollten praktisch
immer eine kunde_id haben). Vorsicht Deploy-Mechanik: eine
Non-Null-Migration im Repo wuerde beim naechsten Container-Neustart
automatisch scharf — nie „auf Vorrat" anlegen. Der sven-jantos-Fall
bleibt vorerst offen (Henrik: „weiss ich nicht") — vor der
Fallback-Entfernung klaeren, sonst ist die Drive-Zeile im Archiv
nicht mehr ueber den Namen auffindbar.

### Phase 8 — Onboarding-Import + Merge-Skript

Ein neuer Tenant bringt seinen Kundenstamm mit — heute gäbe es dafür keinen
Ort, mit der `kunden`-Tabelle gibt es einen. Unabhängig von Phase 5–7
deploybar (braucht nur Phase 1 + den Service); wird spätestens beim ersten
Tenant mit Lexware-Bestand gebraucht.

**Import-Skript `scripts/import_lexware_contacts.py`**, Teil des Onboardings:

- **Richtung:** einmalig Lexware → Gewerbeagent. Danach führt Gewerbeagent;
  Rückschreiben läuft über den ID-basierten `upsert_customer_contact()` aus
  Phase 4. Kein kontinuierlicher Zwei-Wege-Sync — Lexware hat keine
  Webhooks, Konflikterkennung wäre eine eigene Baustelle, und der
  Anwendungsfall braucht sie nicht.
- **Idempotent über `kunde_external_ref`:** existiert schon ein Ref
  `("lexware", contact_id)`, wird der Kontakt übersprungen. Wiederholbar.
- **Zusammenführen statt Neuanlage:** existiert der Kunde bereits über die
  Lookup-Kaskade des Service (z. B. weil er vor dem Import schon eine
  Anfrage gestellt hat), wird nur der Ref angehängt — kein zweiter Kunde.
- **Konflikte wie beim Backfill:** zwei Lexware-Kontakte mit derselben Mail,
  oder Slug-Kollision ohne Mail/Tel → `needs_review=true` + Report-Zeile.
  **Rät nie.**
- **Mapping:** `company.name` bzw. `firstName + lastName` → `name`; erste
  Mail nach Priorität business > office > private > other → `email` (Muster
  existiert in `lexware.py`, `_contact_from_data`); Billing-Adresse →
  `adresse`. Die Reduktion ist verlustbehaftet, aber okay — die Wahrheit
  bleibt über die `external_id` erreichbar.
- **Rate-Limit:** Lexware erlaubt 2 req/s → Drossel wie im
  `rechnung_payment_monitor` (0.3–0.5 s Pause). Ein paar hundert Kontakte
  dauern Minuten — unkritisch, weil einmalig beim Onboarding.

**Merge-Skript `scripts/merge_kunden.py`** (Kunde B geht in Kunde A auf) —
gebraucht für die `needs_review`-Auflösung und Import-Nacharbeit:

- hängt die `kunde_id`-FKs aller acht Bestandstabellen von B auf A um,
- verschiebt `kunde_external_ref`-Zeilen (hat A schon einen Ref im selben
  `system`: abbrechen und melden, nicht überschreiben),
- übernimmt Felder, die A fehlen (Mail, Telefon, Adresse),
- löscht B **nicht** (additive-only), sondern setzt `merged_into_id = A`;
  Lookups im Service **folgen** `merged_into_id` bis zum Ziel (siehe
  Service-Abschnitt — überspringen würde den Create-Zweig auslösen und
  am Unique-Constraint scheitern). Ist A selbst schon gemergt, wird die
  Kette vorab aufgelöst und `merged_into_id` direkt aufs finale Ziel
  gesetzt — so bleiben Ketten einstufig.

---

## Aufwand & Reihenfolge

- **Phase 1–2:** schnell und ungefährlich (rein additiv, kein Bestandspfad
  ändert sich).
- **Phase 3 + 5:** der eigentliche Aufwand — viele Schreib-/Lesestellen, aber
  mechanisch.
- **Phase 6:** der wirksamste Einzelhebel gegen *neue* Vermischungen; kann
  unabhängig von A/B vorgezogen werden.
- **Phase 8:** entkoppelt von 5–7 (braucht nur Phase 1 + Service). Kein
  Blocker für den laufenden Betrieb, aber Voraussetzung fürs Onboarding
  des ersten Tenants mit Lexware-Bestand — rechtzeitig davor bauen.

---

## Relevante Bestands-Fundstellen

- `_kunde_identity_key()` — `core/integrations/google_drive.py:352`
  (Mail > Tel > Slug), heute nur beim Drive-Ordner benutzt.
- `TenantKundeDrive` — `core/models/tenant_kunde_drive.py`; bereits eine
  externe Mapping-Tabelle mit `kunde_key`. Wird in Phase 7 an `kunden.id`
  gehängt.
- `rechnungen.lexware_contact_id` — `core/models/rechnung.py:106`; Lexware-ID
  liegt heute *pro Rechnung*, gesetzt in `telegram_notify/handler.py:7242`.
  Wird in Phase 2/4 nach `kunde_external_ref` konsolidiert.
- `kundengespraeche.kunde_kontakt_id` — `core/models/kundengespraech.py:46`;
  **totes Feld** (UUID, nullable): wird nirgends beschrieben oder gelesen,
  vermutlich ein früherer Anlauf in dieselbe Richtung. **Nicht
  wiederverwenden** — das neue Feld heißt wie überall `kunde_id`, damit
  die acht Tabellen einheitlich bleiben. `kunde_kontakt_id` bleibt stehen
  (additive-only) und wird ignoriert; hier erwähnt, damit beim
  Implementieren niemand versehentlich das falsche Feld befüllt.
- Aktueller Alembic-Head: `d4f8a1c62b7e`.

---

## Offene Punkte (kein Blocker für Phase 1)

- [ ] `adresse` schon in Phase 1 aufnehmen oder später nachziehen?
      (Phase 8 mappt die Lexware-Billing-Adresse dorthin — Empfehlung:
      gleich in Phase 1, erspart eine Nachzieh-Migration vor dem Import.)
- [ ] Soll der Drive-Ordner-Anzeigename den Lesehilfe-Zusatz bekommen
      (rein kosmetisch, siehe Bestandsaufnahme, offene Entscheidung 3)?
- [x] Manuelle Prüfung der `needs_review`-Fälle: gelöst über die
      **Zusammenführen-Karte im Kundenprofil der PWA** (2026-07-17):
      zeigt namensgleiche Dubletten mit Unterscheidungsmerkmal,
      Inhaber-only, `POST /app/api/kunden/merge`. Die Merge-Kernlogik
      wurde dafür aus dem CLI-Skript nach
      `core/services/kunde_merge.py` gezogen (Skript ist jetzt ein
      dünner Wrapper); der Endpoint löscht das Review-Flag am Ziel
      automatisch, wenn keine weitere Namensdublette übrig bleibt.
      Report-CSV (Backfill/Import) bleibt als Ergänzung.
