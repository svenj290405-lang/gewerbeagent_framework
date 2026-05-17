"""
Excel -> Kalkulationsregeln Importer.

Liest eine .xlsx-Datei vom Handwerker und extrahiert pro Zelle mit Formel
einen Eintrag der Form:

    {
        "name":   "Anfahrtspauschale",   # aus Nachbarzelle (Label)
        "formel": "entfernung_km * 0.50", # Excel-Formel ins Pythonische uebersetzt
        "variablen": ["entfernung_km"],
        "raw_excel": "=B2*0,5",          # Original-Formel zur Doku
        "cell":    "C2",
        "sheet":   "Tabelle1",
    }

Die Excel-Formel wird "best effort" in eine Kalkulations-taugliche Python-
Formel uebersetzt:
  - Zellreferenzen (A1, B$2, ...) -> Variable nach Header der Spalte oder
    nach Label der Zeile, ggf. snake_cased.
  - Komma-Dezimal "0,5" -> "0.5".
  - Excel-Funktionen WENN/MIN/MAX/RUNDEN -> Python-Pendant.
  - Operatoren ^, =, <>, & werden uebersetzt bzw. abgelehnt.

Bei Cells, die nicht uebersetzbar sind (z.B. SVERWEIS, INDIREKT), wird der
Eintrag uebersprungen und zu einer Warnung gesammelt.
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Excel-Funktionsname (DE/EN) -> Python-Funktionsname (muss in
# core.ai.kalkulation.SAFE_FUNCTIONS existieren).
_EXCEL_FUNCS = {
    "MIN": "min",
    "MAX": "max",
    "ABS": "abs",
    "RUNDEN": "round",
    "ROUND": "round",
    "AUFRUNDEN": "ceil",
    "ABRUNDEN": "floor",
    "ROUNDUP": "ceil",
    "ROUNDDOWN": "floor",
    "GANZZAHL": "int",
    "INT": "int",
    # WENN/IF behandeln wir gesondert (3 Args -> Python ternary)
}

# Regex fuer eine Excel-Zellreferenz. Optional mit Sheet-Praefix:
# - `B2`                — Same-Sheet
# - `VK!B2`             — Cross-Sheet ohne Spaces
# - `'Sheet Name'!B2`   — Cross-Sheet mit Spaces (Excel-Quotes)
# Group 1 = Sheet-Name (oder None), Group 2 = Cell-Ref.
_CELL_RE = re.compile(
    r"(?<![A-Za-z_])"
    r"(?:([A-Za-z_][A-Za-z0-9_]*|'[^']+')!)?"
    r"(\$?[A-Z]+\$?\d+)"
    r"(?![A-Za-z_0-9])"
)
_RANGE_RE = re.compile(r"\$?[A-Z]+\$?\d+:\$?[A-Z]+\$?\d+")


@dataclass
class ExcelFormelEintrag:
    name: str
    formel: str
    variablen: list[str]
    raw_excel: str
    cell: str
    sheet: str
    # Falls ein Default-Wert in der Original-Quellzelle steht (z.B. "10" als
    # Test-Eingabe), als Vorschlag fuer Variablen-Default mitfuehren.
    variable_defaults: dict[str, float] = field(default_factory=dict)


@dataclass
class ExcelImportResult:
    eintraege: list[ExcelFormelEintrag]
    warnungen: list[str]
    sheets_gelesen: list[str]
    # Wieviele Formeln wurden vom Quality-Filter verworfen, gruppiert
    # nach Grund. Schluessel = Kurz-Code (constant, cell_ref, duplicate),
    # Wert = Anzahl. Wird in der Telegram-Preview als Counts gezeigt,
    # damit der Handwerker versteht warum nicht alle Zellen-Formeln
    # uebernommen wurden.
    verworfen_counts: dict[str, int] = field(default_factory=dict)
    # Wieviele Formeln waren technisch valide aber wurden trotzdem
    # nicht aufgenommen, falls der Handwerker nachvollziehen will
    # wieviele Formeln "knapp daneben" lagen.
    technisch_extrahiert: int = 0


class ExcelImportError(Exception):
    """Datei konnte nicht gelesen werden."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify_label(label: str) -> str:
    """'Entfernung (km)' -> 'entfernung_km'. Fuer Variablennamen."""
    if not label:
        return ""
    # Umlaute / Akzente weg
    s = unicodedata.normalize("NFKD", str(label))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    s = s.replace("Ä", "ae").replace("Ö", "oe").replace("Ü", "ue")
    s = s.replace("ß", "ss").lower()
    # Alles ausser a-z0-9 -> _
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    if not s:
        return ""
    # Nicht mit Ziffer beginnen
    if s[0].isdigit():
        s = "x_" + s
    return s


def _col_letter_to_idx(letters: str) -> int:
    """'A' -> 0, 'B' -> 1, 'AA' -> 26"""
    letters = letters.upper().replace("$", "")
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _split_cell_ref(ref: str) -> tuple[int, int]:
    """'B$3' -> (col_idx=1, row_idx=2). 0-basiert."""
    ref = ref.replace("$", "")
    m = re.match(r"([A-Z]+)(\d+)", ref.upper())
    if not m:
        raise ValueError(f"Keine Zellreferenz: {ref}")
    return _col_letter_to_idx(m.group(1)), int(m.group(2)) - 1


def _label_for_cell(
    grid: list[list[Any]],
    col: int,
    row: int,
) -> str | None:
    """
    Suche eine sinnvolle Beschriftung fuer eine Datenzelle:
    1. Zelle direkt links davon (gleiche Zeile, vorherige Spalten),
    2. Zelle direkt darueber (gleiche Spalte, vorherige Zeilen),
    Erste Text-Zelle (kein Number, keine Formel), gibt Slug zurueck.
    """
    # Nach links suchen
    for c in range(col - 1, -1, -1):
        if row < len(grid) and c < len(grid[row]):
            v = grid[row][c]
            if isinstance(v, str) and v.strip() and not v.startswith("="):
                slug = _slugify_label(v)
                if slug:
                    return slug
    # Nach oben suchen
    for r in range(row - 1, -1, -1):
        if r < len(grid) and col < len(grid[r]):
            v = grid[r][col]
            if isinstance(v, str) and v.strip() and not v.startswith("="):
                slug = _slugify_label(v)
                if slug:
                    return slug
    return None


# ---------------------------------------------------------------------------
# Excel-Formel -> Python-Formel
# ---------------------------------------------------------------------------


def _translate_excel_formula(
    excel_formula: str,
    cell_to_var: dict[str, str],
    current_sheet: str = "",
) -> str:
    """
    Uebersetzt eine Excel-Formel in einen Python-Ausdruck, den unsere
    Kalkulations-Engine versteht.

    cell_to_var: GLOBALES Mapping ueber alle Sheets. Keys haben das
        Format "{SheetName}!{CellRef}" — z.B. {"VK!B2": "preis_standard",
        "Maske!C3": "kunde_wahl"}. Cell-Refs ohne Sheet-Prefix in der
        Formel werden im current_sheet aufgeloest.
    current_sheet: Sheet-Name der Formel-Quelle (fuer implizite
        Same-Sheet-Lookups).
    """
    f = excel_formula.lstrip("=").strip()

    # Locale normalisieren: DE-Excel benutzt ";" als Argument-Trenner und
    # "," als Decimal; EN-Excel umgekehrt.
    # Heuristik (deutsche Handwerker = unsere Zielgruppe):
    #   - ";" drin                      -> DE
    #   - "," zwischen zwei Ziffern     -> DE (Decimal-Komma)
    #   - sonst                         -> EN
    # In DE-Mode tauschen wir "," (Decimal) -> "." und dann ";" (Trenner) -> ",".
    de_mode = (";" in f) or (re.search(r"\d,\d", f) is not None)
    if de_mode:
        f = f.replace(",", ".").replace(";", ",")

    # Bereiche (B2:B5) lehnen wir ab - die kann unsere Sandbox nicht
    if _RANGE_RE.search(f):
        raise ValueError("Zellbereiche (z.B. B2:B5) werden nicht unterstuetzt.")

    # Excel-spezifische Konstrukte: SVERWEIS, VLOOKUP, INDIREKT, INDEX
    for verboten in ("SVERWEIS", "VLOOKUP", "INDIREKT", "INDEX", "MATCH"):
        if re.search(rf"\b{verboten}\b", f, re.IGNORECASE):
            raise ValueError(f"Funktion {verboten} wird nicht unterstuetzt.")

    # WENN(bed, dann, sonst) -> ((dann) if (bed) else (sonst)).
    # Ab hier sind alle Trenner einheitlich Komma (Locale wurde oben
    # normalisiert), und alle Decimals einheitlich Punkt.
    f = _translate_if(f)

    # Funktionsnamen ersetzen (case-insensitive)
    for excel_name, py_name in _EXCEL_FUNCS.items():
        f = re.sub(rf"\b{excel_name}\b", py_name, f, flags=re.IGNORECASE)

    # Excel-Operatoren
    f = f.replace("^", "**")
    f = f.replace("<>", "!=")
    # = (einfach) als Vergleich -> ==. Zuweisungen gibts in Excel-Formeln
    # nicht. Wir wandeln nur einsame "=" um, nicht "==", "<=", ">=", "!=".
    f = re.sub(r"(?<![<>=!])=(?!=)", "==", f)

    # Zellreferenzen -> Variablen. Sheet-Prefix optional, Default ist
    # current_sheet (impliziter Same-Sheet-Lookup).
    def _repl(m: re.Match[str]) -> str:
        sheet_raw = m.group(1)
        ref = m.group(2).replace("$", "")
        sheet = sheet_raw.strip("'") if sheet_raw else current_sheet
        key = f"{sheet}!{ref}"
        if key in cell_to_var:
            return cell_to_var[key]
        # Backward-Kompat: alte Tests rufen ohne current_sheet auf —
        # dort liegt die Map mit reinen Cell-Keys ohne Sheet-Prefix.
        if not current_sheet and ref in cell_to_var:
            return cell_to_var[ref]
        if sheet_raw:
            raise ValueError(
                f"Zelle {sheet}!{ref} hat keinen erkennbaren Namen (Label)"
            )
        raise ValueError(f"Zelle {ref} hat keinen erkennbaren Namen (Label)")

    f = _CELL_RE.sub(_repl, f)

    return f.strip()


def _translate_if(formula: str) -> str:
    """
    WENN(bed; dann; sonst) -> ((dann) if (bed) else (sonst))
    IF(...) wird genauso behandelt.

    Naive Klammer-Tiefen-Suche, weil wir in Phase 1 keine Pyparsing-Dep
    wollen.
    """
    # case-sensitive: Excel-Original ist WENN/IF gross; das Replacement
    # benutzt Python-`if` klein, so dass das Pattern nicht erneut matcht.
    pattern = re.compile(r"\b(WENN|IF)\s*\(")
    while True:
        m = pattern.search(formula)
        if not m:
            return formula
        start_call = m.start()
        # Argument-Liste finden
        depth = 0
        i = m.end() - 1  # zeigt auf '('
        args_start = m.end()
        in_func_end = -1
        for j in range(i, len(formula)):
            c = formula[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    in_func_end = j
                    break
        if in_func_end < 0:
            raise ValueError("WENN/IF: Klammer nicht geschlossen")

        inner = formula[args_start:in_func_end]
        # Args splitten - Trennzeichen ; oder , auf Top-Level
        args = _split_top_level_args(inner)
        # Excel erlaubt sowohl 3-Arg IF(cond, then, else) als auch
        # 2-Arg IF(cond, then). Bei 2 Args ist die false-Branch in
        # Excel implizit FALSE — in unserem numerischen Kontext = 0.
        # Daniels Tabelle nutzt das in Form von verschachtelten IFs:
        # IF(OR(...), "U-Profil", IF(OR(...), "L-Profil")) wo das
        # innere IF die 2-Arg-Kurzform ist.
        if len(args) == 2:
            bed, dann = (a.strip() for a in args)
            sonst = "0"
        elif len(args) == 3:
            bed, dann, sonst = (a.strip() for a in args)
        else:
            raise ValueError(
                f"WENN/IF erwartet 2 oder 3 Argumente, hat {len(args)}"
            )
        # rekursiv weiter aufloesen, falls verschachtelt
        bed = _translate_if(bed)
        dann = _translate_if(dann)
        sonst = _translate_if(sonst)
        replacement = f"(({dann}) if ({bed}) else ({sonst}))"
        formula = formula[:start_call] + replacement + formula[in_func_end + 1:]


def _split_top_level_args(s: str) -> list[str]:
    """Trennt s an ',' auf Klammertiefe 0. Locale wurde vorher normalisiert."""
    args: list[str] = []
    depth = 0
    cur: list[str] = []
    for c in s:
        if c == "(":
            depth += 1
            cur.append(c)
        elif c == ")":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if cur:
        args.append("".join(cur))
    return args


# ---------------------------------------------------------------------------
# Hauptfunktion
# ---------------------------------------------------------------------------


# Skip-Filter-Codes — werden in result.verworfen_counts hochgezaehlt
# damit die Telegram-Preview gruppiert ausgeben kann warum Formeln
# nicht in der Endliste landen.
SKIP_REASON_CONSTANT = "constant"          # Formel hat keine Variablen
SKIP_REASON_CELL_REF = "cell_ref"          # Formel ist eine einzige Cell-Ref
SKIP_REASON_DUPLICATE = "duplicate"        # Gleicher pretty_name + Formel
SKIP_REASON_STRING_OP = "string_op"        # Excel & = String-Concat (=A1&":")
SKIP_REASON_LABELS = {
    SKIP_REASON_CONSTANT: "nur Konstante, keine Variable",
    SKIP_REASON_CELL_REF: "reine Zell-Referenz",
    SKIP_REASON_DUPLICATE: "Duplikat (Name+Formel kommen mehrfach vor)",
    SKIP_REASON_STRING_OP: "String-Concatenation (nicht numerisch)",
}


def _has_string_concat(excel_formula: str) -> bool:
    """True wenn die Formel den Excel-`&`-Operator (String-Concat) ausserhalb
    von Stringliteralen verwendet. Solche Formeln sind Beiwerk fuer Beschriftung
    (z.B. `=VK!B23&":"`) und keine Berechnung — wir skippen sie statt zu
    versuchen sie zu uebersetzen."""
    in_str = False
    for ch in excel_formula:
        if ch == '"':
            in_str = not in_str
        elif ch == "&" and not in_str:
            return True
    return False


def _is_pure_cell_ref(py_formula: str, variablen: list[str]) -> bool:
    """True wenn die Formel exakt einer Variable entspricht (Bsp:
    `=B23` wird zu `stufenzahl` — das ist keine Berechnung sondern
    nur ein Alias auf eine andere Zelle und gehoert nicht in die
    Kalkulationsregel-Liste)."""
    return len(variablen) == 1 and py_formula.strip() == variablen[0]


def extract_formulas_from_xlsx(
    file_bytes: bytes,
    *,
    max_eintraege: int = 200,
) -> ExcelImportResult:
    """
    Liest .xlsx, extrahiert pro Sheet alle Zellen mit Formel und
    uebersetzt sie in Kalkulations-taugliche Python-Formeln.

    Quality-Filter (siehe SKIP_REASON_*): verwirft Konstanten-Formeln,
    reine Zell-Referenzen, und Duplikate (gleicher pretty_name + gleiche
    Formel kommen oft bei komplexen Tabellen mit Block-Layout vor —
    z.B. "Standard" als Label fuer 3 zusammengehoerige Berechnungen).
    """
    try:
        import openpyxl  # noqa: PLC0415 (lazy: optional dep nur fuer Excel)
    except ImportError as exc:
        raise ExcelImportError(
            "openpyxl ist nicht installiert. Bitte `uv sync` ausfuehren."
        ) from exc

    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(file_bytes),
            data_only=False,  # Formeln statt berechneter Werte
            read_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise ExcelImportError(f"Excel-Datei nicht lesbar: {exc}") from exc

    eintraege: list[ExcelFormelEintrag] = []
    warnungen: list[str] = []
    sheets_gelesen: list[str] = []
    verworfen_counts: dict[str, int] = {}
    technisch_extrahiert = 0
    # (pretty_name_lower, py_formula) -> Anzahl der bereits gesehenen
    # Treffer. Wird fuer Dedup und Name-Suffix (#2, #3 ...) benutzt.
    seen_name_formel: dict[tuple[str, str], int] = {}
    # pretty_name_lower -> wie oft wurde der Name vergeben (auch ueber
    # unterschiedliche Formeln hinweg) — Basis fuer den Anzeige-Suffix.
    name_counter: dict[str, int] = {}

    def _bump_skip(reason: str) -> None:
        verworfen_counts[reason] = verworfen_counts.get(reason, 0) + 1

    # Sheets als Grids materialisieren (Pass 1 + Pass 2 brauchen beide
    # alle Grids — bei Cross-Sheet-Refs muss VK!B23 aufloesbar sein
    # auch wenn die Formel im Sheet "Maske" steht).
    sheet_grids: dict[str, list[list[Any]]] = {}
    for ws in wb.worksheets:
        sheets_gelesen.append(ws.title)
        grid: list[list[Any]] = []
        for row in ws.iter_rows(values_only=True):
            grid.append(list(row))
        sheet_grids[ws.title] = grid

    # Pass 1 GLOBAL: jede Zahlen-Zelle ueber alle Sheets bekommt einen
    # eindeutigen Variable-Namen (Label-basiert, mit Slug-Dedup ueber
    # alle Sheets damit dieselbe "Preis"-Spalte aus 2 Sheets nicht
    # kollidiert). Key-Format: "{Sheet}!{Cell}".
    cell_to_var: dict[str, str] = {}
    used_vars: set[str] = set()
    for sheet_name, grid in sheet_grids.items():
        for row_idx, row in enumerate(grid):
            for col_idx, value in enumerate(row):
                if value is None:
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    label = _label_for_cell(grid, col_idx, row_idx)
                    if label:
                        unique = label
                        n = 2
                        while unique in used_vars:
                            unique = f"{label}_{n}"
                            n += 1
                        col_letters = _idx_to_col(col_idx)
                        ref = f"{col_letters}{row_idx + 1}"
                        cell_to_var[f"{sheet_name}!{ref}"] = unique
                        used_vars.add(unique)

    # Pass 2 pro Sheet — Formeln uebersetzen
    for sheet_name, grid in sheet_grids.items():
        ws_title = sheet_name  # Aliase damit der Rest des Codes unveraendert bleibt
        for row_idx, row in enumerate(grid):
            for col_idx, value in enumerate(row):
                if not isinstance(value, str):
                    continue
                if not value.startswith("="):
                    continue

                col_letters = _idx_to_col(col_idx)
                ref = f"{col_letters}{row_idx + 1}"

                # Skip-Filter VOR der Uebersetzung: Excel-String-Concat
                # `&` schickt uns sonst nur in den ValueError-Pfad
                # (Python-Parser kennt das nicht). Hunderte solcher
                # Eintraege in Daniels Tabelle.
                if _has_string_concat(value):
                    technisch_extrahiert += 1
                    _bump_skip(SKIP_REASON_STRING_OP)
                    continue

                try:
                    py_formula = _translate_excel_formula(
                        value, cell_to_var, current_sheet=ws_title,
                    )
                except ValueError as exc:
                    warnungen.append(
                        f"{ws_title}!{ref}: {exc} (Original: {value})"
                    )
                    continue

                # Validierung gegen Sandbox
                try:
                    from core.ai.kalkulation import (  # noqa: PLC0415
                        FormelError,
                        parse_variables,
                    )

                    variablen = parse_variables(py_formula)
                except FormelError as exc:
                    warnungen.append(
                        f"{ws_title}!{ref}: Formel ungueltig ({exc}). "
                        f"Original: {value}"
                    )
                    continue

                technisch_extrahiert += 1

                # Quality-Filter 1: Konstanten-Formeln (z.B. `1/60`)
                # bringen als Kalkulationsregel nichts — keine Variable
                # heisst nichts vom Handwerker zu uebergeben.
                if not variablen:
                    _bump_skip(SKIP_REASON_CONSTANT)
                    continue

                # Quality-Filter 2: Reine Zell-Referenzen (`=B23` -> `stufenzahl`)
                # sind Aliase und keine Berechnungen.
                if _is_pure_cell_ref(py_formula, variablen):
                    _bump_skip(SKIP_REASON_CELL_REF)
                    continue

                pretty_name = _pretty_label_for_cell(grid, col_idx, row_idx) or ref

                # Quality-Filter 3: Duplikate (gleicher pretty_name +
                # gleiche Formel) verwerfen. Beispiel: Excel-Block mit
                # 3x "Standard"-Spalte deren Formeln identisch sind.
                dedup_key = (pretty_name.lower(), py_formula)
                if dedup_key in seen_name_formel:
                    _bump_skip(SKIP_REASON_DUPLICATE)
                    continue
                seen_name_formel[dedup_key] = 1

                # Suffix-Nummerierung wenn Name (case-insensitive) schon
                # vergeben aber Formel unterschiedlich war: "Standard",
                # "Standard #2", "Standard #3" — so kann der Handwerker
                # die Eintraege in der Liste unterscheiden.
                name_key = pretty_name.lower()
                name_counter[name_key] = name_counter.get(name_key, 0) + 1
                if name_counter[name_key] > 1:
                    pretty_name = f"{pretty_name} #{name_counter[name_key]}"

                # Default-Werte aus den Quellzellen einsammeln. cell_to_var
                # hat jetzt "Sheet!Cell"-Keys — wir muessen das Sheet aus
                # dem Key parsen um in das richtige sheet_grids[s] zu greifen.
                variable_defaults: dict[str, float] = {}
                for var in variablen:
                    for cref_keyed, vname in cell_to_var.items():
                        if vname != var:
                            continue
                        if "!" in cref_keyed:
                            src_sheet, src_ref = cref_keyed.split("!", 1)
                        else:
                            src_sheet, src_ref = ws_title, cref_keyed
                        src_grid = sheet_grids.get(src_sheet)
                        if src_grid is None:
                            break
                        try:
                            ccol, crow = _split_cell_ref(src_ref)
                            if crow < len(src_grid) and ccol < len(src_grid[crow]):
                                val = src_grid[crow][ccol]
                                if isinstance(val, (int, float)) and not isinstance(val, bool):
                                    variable_defaults[var] = float(val)
                        except (ValueError, IndexError):
                            pass
                        break

                eintraege.append(
                    ExcelFormelEintrag(
                        name=pretty_name,
                        formel=py_formula,
                        variablen=variablen,
                        raw_excel=value,
                        cell=ref,
                        sheet=ws_title,
                        variable_defaults=variable_defaults,
                    )
                )

                if len(eintraege) >= max_eintraege:
                    warnungen.append(
                        f"Limit von {max_eintraege} Formeln erreicht - "
                        "weitere Zellen wurden ignoriert."
                    )
                    return ExcelImportResult(
                        eintraege=eintraege,
                        warnungen=warnungen,
                        sheets_gelesen=sheets_gelesen,
                        verworfen_counts=verworfen_counts,
                        technisch_extrahiert=technisch_extrahiert,
                    )

    return ExcelImportResult(
        eintraege=eintraege,
        warnungen=warnungen,
        sheets_gelesen=sheets_gelesen,
        verworfen_counts=verworfen_counts,
        technisch_extrahiert=technisch_extrahiert,
    )


def _idx_to_col(idx: int) -> str:
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA'."""
    out = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _row_label_for_cell(
    grid: list[list[Any]], col: int, row: int,
) -> str | None:
    """Erste Text-Zelle LINKS in der gleichen Zeile (Zeilen-Beschriftung)."""
    for c in range(col - 1, -1, -1):
        if row < len(grid) and c < len(grid[row]):
            v = grid[row][c]
            if isinstance(v, str) and v.strip() and not v.startswith("="):
                return v.strip()
    return None


def _column_header_for_cell(
    grid: list[list[Any]], col: int, row: int,
) -> str | None:
    """Erste Text-Zelle OBEN in der gleichen Spalte (Spalten-Header).

    Sucht von Zeile 0 abwaerts statt direkt-darueber, damit der echte
    Header (typisch Zeile 1 oder 2) gefunden wird, nicht ein anderer
    Wert der zufaellig in einer Block-Tabelle ueber der Formel-Zelle steht.
    """
    for r in range(row):
        if r < len(grid) and col < len(grid[r]):
            v = grid[r][col]
            if isinstance(v, str) and v.strip() and not v.startswith("="):
                return v.strip()
    return None


def _pretty_label_for_cell(
    grid: list[list[Any]],
    col: int,
    row: int,
) -> str | None:
    """Kombiniert Spalten-Header und Zeilen-Label fuer Block-Tabellen.

    Block-Layout-Beispiel (Daniels Treppen-Kalkulation):
        |          | Standard | Komfort | Premium |   <- Zeile 1 = Spalten-Header
        | Material |   100    |   150   |   200   |
        | Summe    | =B2*1.2  | =C2*1.2 | =D2*1.2 |

    Vorher: jede Summen-Zelle nahm nur das LINKS-Label "Summe" — alle
    drei waren "Summe", "Summe #2", "Summe #3" und der Handwerker
    wusste nicht welche zu welcher Treppen-Variante gehoert.

    Jetzt: pretty_name = "Spalten-Header — Zeilen-Label" wenn beide da:
    "Standard — Summe", "Komfort — Summe", "Premium — Summe".

    Wenn nur einer existiert: nur den nehmen. Wenn keiner: None
    (Caller faellt zurueck auf Cell-Ref wie "B5").
    """
    row_label = _row_label_for_cell(grid, col, row)
    col_header = _column_header_for_cell(grid, col, row)

    # Wenn Header und Row-Label gleich sind (kann passieren bei
    # diagonal-Layout), nur einen nehmen.
    if row_label and col_header and row_label.lower() == col_header.lower():
        return row_label

    if row_label and col_header:
        return f"{col_header} — {row_label}"
    return row_label or col_header
