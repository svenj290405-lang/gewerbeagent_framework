"""Tests fuer Excel-Kalkulation Quality-Filter (2026-05-17).

Hintergrund: Komplexe Tabellen mit Block-Layout (z.B. 3x "Standard"-
Spalte, viele Cell-Aliases, Konstanten als Formeln) haben vorher
hunderte "Formeln" produziert die kein Mensch wiederfindet. Der Filter
verwirft:
- Konstanten-Formeln (`1/60`, `=2*PI()`)
- Reine Zell-Referenzen (`=B23` -> `stufenzahl`)
- Duplikate (gleicher Label + gleiche Formel)

Bei Namens-Duplikaten mit unterschiedlichen Formeln wird der Anzeige-
Name mit "#2", "#3" suffixiert.
"""
from __future__ import annotations

import io

import pytest

from core.integrations.excel_kalkulation import (
    SKIP_REASON_CELL_REF,
    SKIP_REASON_CONSTANT,
    SKIP_REASON_DUPLICATE,
    SKIP_REASON_LABELS,
    ExcelFormelEintrag,
    extract_formulas_from_xlsx,
)


# =====================================================================
# Helper: synthetische xlsx-Datei aus Sheet-Daten bauen
# =====================================================================

def _make_xlsx(
    rows: list[list],
    sheet_name: str = "Tabelle1",
) -> bytes:
    """Baut eine .xlsx-Datei mit den gegebenen Zellen-Werten.

    String-Werte die mit '=' beginnen werden als Excel-Formel gespeichert
    (openpyxl interpretiert das beim Write-Time, beim Read-Back zurueck
    bekommen wir den Original-String inkl '=').
    """
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for r_idx, row in enumerate(rows, start=1):
        for c_idx, val in enumerate(row, start=1):
            ws.cell(row=r_idx, column=c_idx, value=val)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =====================================================================
# Skip-Filter: Konstanten
# =====================================================================

def test_constant_formula_is_skipped():
    """`=1/60` hat keine Variable -> verworfen."""
    xlsx = _make_xlsx([
        ["SVS/sec:", "=1/60"],
    ])
    result = extract_formulas_from_xlsx(xlsx)
    assert result.eintraege == []
    assert result.verworfen_counts.get(SKIP_REASON_CONSTANT) == 1
    assert result.technisch_extrahiert == 1


def test_arithmetic_constant_is_skipped():
    """`=1.47/60` ist eine Berechnung aber liefert keine Variable -> raus."""
    xlsx = _make_xlsx([
        ["Konstante:", "=1.47/60"],
        ["Andere:", "=3*4+5"],
    ])
    result = extract_formulas_from_xlsx(xlsx)
    assert result.eintraege == []
    assert result.verworfen_counts.get(SKIP_REASON_CONSTANT) == 2


# =====================================================================
# Skip-Filter: Reine Zell-Referenzen
# =====================================================================

def test_pure_cell_reference_is_skipped():
    """`=A1` ist nur Alias -> verworfen statt als "Formel" gespeichert.

    Setup: A1=12 (Number) mit Label "Stufenzahl" in B1 (Label-Suche
    geht zuerst links, dann oben — also B1=Label fuer A1 funktioniert
    nicht; wir setzen das Label deshalb DARUEBER in A0 ist nicht
    moeglich, also unter A1=12 mit Label LINKS in einer separaten Zeile.
    Format: A1="Stufenzahl" (Label), A2=12 (Number, Label-oben = "Stufenzahl"),
    A3="=A2" (reine Cell-Ref auf A2).
    """
    xlsx = _make_xlsx([
        ["Stufenzahl"],  # A1 = Label-String
        [12],            # A2 = Number, Label-Suche oben findet A1 -> slug "stufenzahl"
        ["=A2"],         # A3 = reine Cell-Ref auf A2 = stufenzahl
    ])
    r = extract_formulas_from_xlsx(xlsx)
    assert r.eintraege == []
    assert r.verworfen_counts.get(SKIP_REASON_CELL_REF) == 1


# =====================================================================
# Skip-Filter: Duplikate
# =====================================================================

def test_duplicate_name_and_formula_is_skipped():
    """Gleicher Label + gleiche Formel zweimal -> zweiter Eintrag raus.

    Layout: A1="km" (Label), B1=42 (Number mit Label-links="km").
    A2="Anfahrt" (Label), B2="=B1*2", C2="=B1*2".
    Beide Formel-Zellen liegen rechts von "Anfahrt" -> beide bekommen
    pretty_name="Anfahrt", gleiche Formel -> Dedup.
    """
    xlsx = _make_xlsx([
        ["km", 42],
        ["Anfahrt", "=B1*2", "=B1*2"],
    ])
    result = extract_formulas_from_xlsx(xlsx)
    assert len(result.eintraege) == 1
    assert result.verworfen_counts.get(SKIP_REASON_DUPLICATE) == 1
    # technisch_extrahiert zaehlt beide
    assert result.technisch_extrahiert == 2


# =====================================================================
# Namens-Suffix bei Same-Name-Different-Formula
# =====================================================================

def test_same_name_different_formula_gets_numeric_suffix():
    """Gleicher Label, unterschiedliche Formeln -> "Standard", "Standard #2".

    Layout:
    A1="km" (Label), B1=10 (Number, label-links="km")
    A2="m"  (Label), B2=20 (Number, label-links="m")
    A3="Standard" (Label), B3="=B1*2" (label-links="Standard"),
                            C3="=B2*3" (label-links="Standard")
    """
    xlsx = _make_xlsx([
        ["km", 10],
        ["m", 20],
        ["Standard", "=B1*2", "=B2*3"],
    ])
    result = extract_formulas_from_xlsx(xlsx)
    assert len(result.eintraege) == 2
    names = [e.name for e in result.eintraege]
    assert "Standard" in names
    assert "Standard #2" in names


# =====================================================================
# Happy path: valider Eintrag bleibt drin
# =====================================================================

def test_valid_formula_with_variables_is_kept():
    """Multi-Variablen-Formel mit gutem Label -> wird gespeichert.

    Layout:
    A1="entfernung_km" (Label), B1=50 (Number -> slug "entfernung_km")
    A2="preis_pro_km"  (Label), B2=2  (Number -> slug "preis_pro_km")
    A3="Anfahrtskosten" (Label), B3="=B1*B2" (label-links="Anfahrtskosten")
    """
    xlsx = _make_xlsx([
        ["entfernung_km", 50],
        ["preis_pro_km", 2],
        ["Anfahrtskosten", "=B1*B2"],
    ])
    result = extract_formulas_from_xlsx(xlsx)
    assert len(result.eintraege) == 1
    e = result.eintraege[0]
    assert e.name == "Anfahrtskosten"
    assert set(e.variablen) == {"entfernung_km", "preis_pro_km"}
    assert result.verworfen_counts == {}


# =====================================================================
# Mixed scenario: alle Filter zusammen
# =====================================================================

def test_mixed_real_world_scenario():
    """Komplette Tabelle mit Mix: 1 gut, 1 Duplikat, 1 reine Cell-Ref,
    1 Konstante -> nur die gute kommt durch, counts stimmen.

    Layout:
    A1="km",    B1=50         -> B1 slug="km"
    A2="preis", B2=2          -> B2 slug="preis"
    A3="Anfahrt", B3="=B1*B2", C3="=B1*B2" (dup), D3="=B1" (cell-ref), E3="=1/60" (const)
    """
    xlsx = _make_xlsx([
        ["km", 50],
        ["preis", 2],
        ["Anfahrt", "=B1*B2", "=B1*B2", "=B1", "=1/60"],
    ])
    result = extract_formulas_from_xlsx(xlsx)
    assert len(result.eintraege) == 1
    assert result.eintraege[0].name == "Anfahrt"
    counts = result.verworfen_counts
    assert counts.get(SKIP_REASON_DUPLICATE) == 1
    assert counts.get(SKIP_REASON_CELL_REF) == 1
    assert counts.get(SKIP_REASON_CONSTANT) == 1
    assert result.technisch_extrahiert == 4


# =====================================================================
# Labels-Mapping
# =====================================================================

def test_skip_reason_labels_present_for_all_codes():
    for code in (SKIP_REASON_CONSTANT, SKIP_REASON_CELL_REF, SKIP_REASON_DUPLICATE):
        assert code in SKIP_REASON_LABELS
        assert SKIP_REASON_LABELS[code]  # not empty


# =====================================================================
# Dataclass-Defaults
# =====================================================================

def test_eintrag_has_variable_defaults_field():
    """ExcelFormelEintrag muss variable_defaults haben (fuer Hybrid-Calc)."""
    e = ExcelFormelEintrag(
        name="X", formel="a*b", variablen=["a", "b"],
        raw_excel="=A1*B1", cell="C1", sheet="S1",
    )
    assert e.variable_defaults == {}
