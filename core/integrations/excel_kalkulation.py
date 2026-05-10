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

# Regex fuer eine Excel-Zellreferenz, mit optionalem Sheet-Praefix.
# Wir ignorieren Sheet-Praefix in Phase 1 (gleiches Sheet erwartet).
_CELL_RE = re.compile(r"(?<![A-Za-z_])(\$?[A-Z]+\$?\d+)(?![A-Za-z_0-9])")
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
) -> str:
    """
    Uebersetzt eine Excel-Formel in einen Python-Ausdruck, den unsere
    Kalkulations-Engine versteht.

    cell_to_var: Mapping z.B. {"B2": "entfernung_km", "C3": "stunden"}
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

    # Zellreferenzen -> Variablen
    def _repl(m: re.Match[str]) -> str:
        ref = m.group(1).replace("$", "")
        if ref in cell_to_var:
            return cell_to_var[ref]
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
        if len(args) != 3:
            raise ValueError(
                f"WENN/IF erwartet 3 Argumente, hat {len(args)}"
            )
        bed, dann, sonst = (a.strip() for a in args)
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


def extract_formulas_from_xlsx(
    file_bytes: bytes,
    *,
    max_eintraege: int = 200,
) -> ExcelImportResult:
    """
    Liest .xlsx, extrahiert pro Sheet alle Zellen mit Formel und
    uebersetzt sie in Kalkulations-taugliche Python-Formeln.
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

    for ws in wb.worksheets:
        sheets_gelesen.append(ws.title)

        # Vollen Grid materialisieren (read_only laeuft sonst nur einmal)
        grid: list[list[Any]] = []
        for row in ws.iter_rows(values_only=True):
            grid.append(list(row))

        # 1. Zell -> Slug-Label aus Nachbarschaft
        cell_to_var: dict[str, str] = {}
        for row_idx, row in enumerate(grid):
            for col_idx, value in enumerate(row):
                if value is None:
                    continue
                # Spaltenbuchstaben fuer Excel-Notation
                col_letters = _idx_to_col(col_idx)
                ref = f"{col_letters}{row_idx + 1}"
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    label = _label_for_cell(grid, col_idx, row_idx)
                    if label:
                        # Bei Duplikat-Slug: numerieren
                        unique = label
                        n = 2
                        while unique in cell_to_var.values():
                            unique = f"{label}_{n}"
                            n += 1
                        cell_to_var[ref] = unique

        # 2. Formel-Zellen verarbeiten
        for row_idx, row in enumerate(grid):
            for col_idx, value in enumerate(row):
                if not isinstance(value, str):
                    continue
                if not value.startswith("="):
                    continue

                col_letters = _idx_to_col(col_idx)
                ref = f"{col_letters}{row_idx + 1}"

                try:
                    py_formula = _translate_excel_formula(value, cell_to_var)
                except ValueError as exc:
                    warnungen.append(
                        f"{ws.title}!{ref}: {exc} (Original: {value})"
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
                        f"{ws.title}!{ref}: Formel ungueltig ({exc}). "
                        f"Original: {value}"
                    )
                    continue

                name = _label_for_cell(grid, col_idx, row_idx) or f"formel_{ref}"
                # Schoener Name fuer Anzeige (nicht der Slug)
                pretty_name = _pretty_label_for_cell(grid, col_idx, row_idx) or ref

                # Default-Werte aus den Quellzellen einsammeln
                variable_defaults: dict[str, float] = {}
                for var in variablen:
                    # Reverse-Lookup: welche Cell gehoert zu var?
                    for cref, vname in cell_to_var.items():
                        if vname == var:
                            try:
                                ccol, crow = _split_cell_ref(cref)
                                if crow < len(grid) and ccol < len(grid[crow]):
                                    val = grid[crow][ccol]
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
                        sheet=ws.title,
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
                    )

    return ExcelImportResult(
        eintraege=eintraege,
        warnungen=warnungen,
        sheets_gelesen=sheets_gelesen,
    )


def _idx_to_col(idx: int) -> str:
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA'."""
    out = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _pretty_label_for_cell(
    grid: list[list[Any]],
    col: int,
    row: int,
) -> str | None:
    """Wie _label_for_cell, aber gibt das Original-Label zurueck (nicht Slug)."""
    for c in range(col - 1, -1, -1):
        if row < len(grid) and c < len(grid[row]):
            v = grid[row][c]
            if isinstance(v, str) and v.strip() and not v.startswith("="):
                return v.strip()
    for r in range(row - 1, -1, -1):
        if r < len(grid) and col < len(grid[r]):
            v = grid[r][col]
            if isinstance(v, str) and v.strip() and not v.startswith("="):
                return v.strip()
    return None
