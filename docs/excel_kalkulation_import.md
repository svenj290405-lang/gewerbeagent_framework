# Excel-Kalkulation-Import: Architektur + Bekannte Bugs (Stand 2026-05-17)

Lebende Doku des Excel-Import-Pfads für `/kalkulation_excel`. Schreibt
in **`tenant_kalkulationen`** (NICHT TenantKnowledge — das ist eine
separate Tabelle für FAQ/Besonderheiten ohne Excel-Pfad).

## Code-Pfad

### Telegram-Wizard
`plugins/telegram_notify/handler.py`:
| Handler | State-Transition | Zweck |
|---|---|---|
| `_handle_kalkulation_excel_command` (Z.2061) | → `STATE_KALK_EXCEL_WAITING` | startet Wizard, fragt nach .xlsx |
| `_handle_kalk_excel_received` (Z.2077) | → `STATE_KALK_EXCEL_CONFIRM` | empfängt Datei, parser, zeigt Preview |
| `_handle_kalk_excel_confirm_input` (Z.2169) | clear | bei "ja": speichert ALLE als kategorie="sonstiges" |

### Parser
`core/integrations/excel_kalkulation.py` (openpyxl, `read_only=True`, `data_only=False`)

Pipeline pro Sheet:
1. **Grid materialisieren** (`iter_rows(values_only=True)`)
2. **Pass 1 — Zahlen-Zellen mappen:** Für jede `int|float`-Zelle:
   - `_label_for_cell(grid, col, row)` sucht Beschriftung in Nachbarschaft
   - **erst** alle Spalten LINKS in der gleichen Zeile, **dann** alle Zeilen OBEN in der gleichen Spalte
   - Erste Text-Zelle (kein Number, nicht mit `=` beginnend) → `_slugify_label` → snake_case
   - Duplikat-Slug wird durchnummeriert (`paket`, `paket_2`, `paket_3`, ...)
   - Mapping `cell_to_var["B2"] = "stunden"` aufgebaut
3. **Pass 2 — Formel-Zellen übersetzen:** Für jede String-Zelle die mit `=` startet:
   - `_translate_excel_formula()`:
     - **Locale-Heuristik**: `;` drin oder `\d,\d` → DE-Mode, tauscht `,`→`.` dann `;`→`,`
     - **Reject**: `_RANGE_RE` (B2:B5), SVERWEIS/VLOOKUP/INDIREKT/INDEX/MATCH
     - **`_translate_if`**: WENN/IF rekursiv → Python ternary
       - **HART: erwartet 3 args** — bei 2 args ValueError, bei 4+ auch
     - Funktions-Map: MIN/MAX/ABS/RUNDEN/AUFRUNDEN/ABRUNDEN/GANZZAHL → Python-Pendant
     - Operatoren: `^`→`**`, `<>`→`!=`, einsame `=`→`==`
     - **`_CELL_RE`**: Zell-Refs ohne Sheet-Prefix → `cell_to_var[ref]` oder ValueError
   - `parse_variables()` (AST-Sandbox in `core.ai.kalkulation`)
   - **Quality-Filter (a342f3b)**:
     - `not variablen` → SKIP constant
     - `py_formula == variablen[0]` → SKIP cell_ref
     - `(name.lower(), formel)` schon gesehen → SKIP duplicate
   - **Name-Dedup-Suffix**: wenn name (case-insensitive) doppelt aber Formel anders → `Name #2`, `Name #3`
   - `pretty_name = _pretty_label_for_cell(grid, col, row)` (gleiche LINKS-DANN-OBEN-Suche, original Text)
4. **Result**: `ExcelImportResult(eintraege, warnungen, sheets_gelesen, verworfen_counts, technisch_extrahiert)`

### Persistenz
`core/models/tenant_kalkulation.py` → Tabelle `tenant_kalkulationen`

Spalten die für Auftrag relevant sind:
- `name` — pretty_name (200 chars)
- `formel` — übersetzte Python-Formel (1000 chars)
- `variablen` — ARRAY(String), aus Formel extrahiert
- `kategorie` — String(50), eine von ALLE_KALK_KATEGORIEN
- **`beschreibung`** — Text, nullable → **HIER kann Teil D semantische Beschreibung speichern, keine Migration nötig**
- `einheit` — String(50), nullable
- `aktiv` — Bool, default true (Soft-Delete)
- `source` — "manual" | "excel"
- `excel_filename` — String(255), nullable

**ALLE_KALK_KATEGORIEN** (`anfahrt`, `material`, `stundenlohn`, `aufschlag`, `rabatt`, `pauschale`, `sonstiges`)

## Bekannte Bugs

### B1: `IF` mit 2 Argumenten crash
`_translate_if` Z.272: `if len(args) != 3: raise ValueError`
Excel erlaubt aber `IF(cond, value)` als Kurzform (false-branch = FALSE = 0).
Beispiel aus User-Daten: `IF(OR(C4="Premium",C4="Premium PLUS"),"L-Profil")` — innere IF hat nur 2 args, äußere crashed dadurch komplett.

### B2: Falsches Label durch Spalten-Layout ("Komfort #2"-Bug)
`_pretty_label_for_cell` sucht **erst LINKS, dann OBEN**. Bei einer Block-Tabelle:
```
       B(Standard)  C(Komfort)
9      150          200            ← Zahlen-Zelle mit Label-OBEN = Standard/Komfort
10     =B9+1        =C9+1          ← Formel-Zellen
```
- `B9` (Zahl): `_label_for_cell` findet OBEN B1="Standard" → mapping `B9 → standard_9`
- `C9` (Zahl): OBEN C1="Komfort" → `C9 → komfort_9`
- `B10` (Formel `=B9+1`): `_pretty_label_for_cell` sucht LINKS — A10 ist evtl. "Setzstufe" → `pretty_name = "Setzstufe"`, Formel `standard_9+1` ✅
- `C10` (Formel `=C9+1`): LINKS B10 ist Formel (skip), A10="Setzstufe" → `pretty_name = "Setzstufe"`, Formel `komfort_9+1` ✅

Bei DEINER Tabelle ist es anders: `C10 = =B9+1` (Formel verweist auf NACHBAR-Spalte). Dann:
- `pretty_name` von C10 = "Komfort" (Spalten-Header) ODER aus LINKS-Zelle
- Variablen = `standard_9` (weil B9 zur Standard-Spalte gehört)
- → Anzeige "Komfort: standard_9+1" — semantisch WTF.

**Echte Fix-Richtung**: Wenn die Variablen-Namen einen ANDEREN Spalten-Header tragen als der pretty_name andeutet, sollte der pretty_name den dominanten Variable-Header übernehmen ODER den Block-Header aus der Spalten-Header-Zeile (oberste Text-Zeile) mit anhängen.

### B3: Cross-Sheet-Refs (`VK!B23`) werden nicht aufgelöst
`_CELL_RE = r"(?<![A-Za-z_])(\$?[A-Z]+\$?\d+)(?![A-Za-z_0-9])"` — matched nur die Cell-Ref ohne Sheet-Prefix. Wenn das im aktuellen Sheet kein Mapping hat → "Zelle X hat keinen erkennbaren Namen". In Daniels Tabelle sind die Master-Daten in Sheet "VK" und die Maske referenziert quer-rüber → 200+ solcher Warnings.

**Fix-Richtung**: Cell-Mapping muss alle Sheets sammeln (mit Sheet-Prefix-Key: `"VK!B23" → "preis_standard"`). Cross-Sheet-Refs würden dann aufgelöst.

### B4: Excel-String-Operator `&` nicht übersetzt
`=VK!B23&":"` — Excel-Concat-Operator → Python-Parser bekommt `& ":"` → "Syntax-Fehler: invalid syntax". 27+ solcher Warnings in Daniels Output.

**Fix-Richtung**: Entweder `&` → `+` (Python-String-Concat) ODER als nicht-numerisch erkennen und ganz skippen (String-Concat ist nichts für eine Berechnungs-Formel).

### B5: Kategorisierung pauschal "sonstiges"
`_handle_kalk_excel_confirm_input` Z.2191 hardcoded `kategorie="sonstiges"`. Alle 17+ brauchbare Formeln landen im selben Topf. → Teil C: Gemini-Auto-Klassifizierung gegen ALLE_KALK_KATEGORIEN.

### B6: Keine semantische Beschreibung
`beschreibung`-Spalte wird beim Excel-Import nur mit `f"Aus {filename} ({sheet}!{cell}, Original: {raw_excel})"` befüllt — technisch, nicht semantisch. → Teil D: Gemini erzeugt Klartext-Beschreibung.

### B7: Keine Pro-Eintrag-Bestätigung
`_handle_kalk_excel_confirm_input` macht alles oder nichts ("ja" speichert alle, /abbrechen verwirft alle). → Teil E: Pro-Eintrag-Approval mit /ja, /nein, /skip.

## Top-Warnungs-Klassen aus Daniels Excel-Output

Aus den 270 Warnungen lassen sich 4 Klassen identifizieren:

| # | Pattern | Anzahl (geschätzt) | Beispiel | Fix |
|---|---|---|---|---|
| 1 | Cross-Sheet-Ref ohne Mapping | ~200 | `=VK!B23&":"` | B3 + B4 |
| 2 | Excel-String-Concat `&` | ~30 | `=A1&"€"` | B4 |
| 3 | IF mit 2 Args | ~10 | `=IF(...,"L-Profil")` | B1 |
| 4 | Andere (RUNDEN-Edge, leere Refs) | <30 | div. | später |

## Auftrag-Fix-Plan

| Teil | Was | Status |
|---|---|---|
| A | Diagnose | ✅ (dies hier) |
| B.1 | IF mit 2 Args supporten (false-branch=0) | offen |
| B.2 | Label-Bug bei Spalten-Header-Layout | offen |
| B.3 | Cross-Sheet-Refs (`VK!B23`) auflösen | offen |
| B.4 | `&`-Operator (String-Concat) | offen — wahrscheinlich SKIP statt fix |
| C | Gemini-Auto-Kategorisierung (Batch-Call gegen ALLE_KALK_KATEGORIEN) | offen |
| D | Gemini semantische Beschreibung (Batch-Call) | offen |
| E | Pro-Eintrag-Preview mit /ja /nein | offen |
| F | Tests (≥10) | offen |
