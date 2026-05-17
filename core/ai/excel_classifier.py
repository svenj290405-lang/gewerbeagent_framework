"""Gemini-basierte Auto-Klassifikation + Beschreibung fuer Excel-Import.

Ein Batch-Call fuer alle Formeln eines Imports — pro Formel liefert
Gemini (Kategorie, Beschreibung). Tokens-sparend, ein Round-Trip.

Verwendung:
    from core.ai.excel_classifier import classify_excel_eintraege

    results = await classify_excel_eintraege(
        eintraege=[
            {"name": "Standard — Summe", "formel": "a+b+c",
             "variablen": ["a", "b", "c"], "sheet": "VK"},
            ...
        ],
        tenant_id="uuid-string",
    )
    # results == [
    #   {"kategorie": "pauschale", "beschreibung": "Gesamtpreis ..."},
    #   ...
    # ]
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.ai.gemini import call_gemini
from core.models.tenant_kalkulation import (
    ALLE_KALK_KATEGORIEN,
    KALK_KATEGORIE_LABELS,
    KALK_KATEGORIE_SONSTIGES,
)

logger = logging.getLogger(__name__)


# Maximaler Beschreibungs-Text — wird auch in TenantKalkulation.beschreibung
# (Text-Spalte) gespeichert, hat dort kein Hard-Limit aber wir kappen
# hier damit die Voice-Ansage sinnvoll lang bleibt.
MAX_BESCHREIBUNG_LEN = 200


def _build_prompt(eintraege: list[dict]) -> str:
    """Baut den Batch-Prompt fuer Kategorisierung + Beschreibung."""
    kategorien_liste = "\n".join(
        f"  - {code}: {KALK_KATEGORIE_LABELS[code]}"
        for code in ALLE_KALK_KATEGORIEN
    )
    formeln_liste = "\n".join(
        f"{i + 1}. name={e['name']!r} | formel={e['formel']!r} "
        f"| variablen={e['variablen']!r} | sheet={e.get('sheet', '?')!r}"
        for i, e in enumerate(eintraege)
    )
    return f"""Du analysierst Kalkulationsformeln aus einer Excel-Tabelle
eines Handwerksbetriebs. Fuer jede Formel: bestimme Kategorie + schreibe
einen kurzen deutschen Beschreibungssatz.

KATEGORIEN (genau einer pro Formel):
{kategorien_liste}

BESCHREIBUNG:
- Max {MAX_BESCHREIBUNG_LEN} Zeichen
- Was berechnet die Formel? (in einem Satz, so dass ein Voice-Assistent
  das einem Kunden am Telefon vorlesen koennte)
- Beispiel: "Gesamtpreis einer Standard-Treppe, berechnet aus Material,
  Arbeitszeit und Montage"

FORMELN:
{formeln_liste}

Antworte AUSSCHLIESSLICH mit gueltigem JSON:
{{
  "results": [
    {{"index": 1, "kategorie": "<kategorie-code>", "beschreibung": "<text>"}},
    {{"index": 2, "kategorie": "<kategorie-code>", "beschreibung": "<text>"}},
    ...
  ]
}}
"""


def _strip_code_fences(text: str) -> str:
    """Entfernt Markdown-Code-Fences die Gemini gelegentlich um JSON wickelt."""
    s = text.strip()
    if s.startswith("```"):
        # ```json\n...\n``` oder ```\n...\n```
        s = re.sub(r"^```[a-zA-Z0-9_]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def _parse_gemini_response(text: str, n_expected: int) -> list[dict] | None:
    """Parst Gemini-JSON, gibt geordnete Liste der Resultate (Index 1..N)
    oder None wenn nicht parsbar."""
    try:
        data = json.loads(_strip_code_fences(text))
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("Gemini-JSON nicht parsbar: %s", e)
        return None

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        logger.warning("Gemini-Response ohne 'results'-Liste")
        return None

    # In Index-Reihenfolge bringen — Gemini koennte umsortieren
    by_index: dict[int, dict] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        try:
            idx = int(r.get("index", 0))
        except (TypeError, ValueError):
            continue
        by_index[idx] = r

    ordered = []
    for i in range(1, n_expected + 1):
        ordered.append(by_index.get(i) or {})
    return ordered


def _normalize_one(raw: dict) -> dict:
    """Validiert + clamped einen einzelnen Eintrag aus der Gemini-Response."""
    kat = (raw.get("kategorie") or "").strip().lower()
    if kat not in ALLE_KALK_KATEGORIEN:
        kat = KALK_KATEGORIE_SONSTIGES
    beschreibung = (raw.get("beschreibung") or "").strip()
    if len(beschreibung) > MAX_BESCHREIBUNG_LEN:
        beschreibung = beschreibung[:MAX_BESCHREIBUNG_LEN].rstrip() + "…"
    return {"kategorie": kat, "beschreibung": beschreibung}


def _fallback_results(n: int) -> list[dict]:
    """Wenn Gemini fehlt: alle als 'sonstiges' mit leerer Beschreibung."""
    return [
        {"kategorie": KALK_KATEGORIE_SONSTIGES, "beschreibung": ""}
        for _ in range(n)
    ]


async def classify_excel_eintraege(
    eintraege: list[dict],
    *,
    tenant_id: str | None = None,
) -> list[dict]:
    """Liefert pro Eintrag {kategorie, beschreibung}.

    Gibt IMMER eine Liste gleicher Laenge zurueck (auch bei Fehler —
    Fallback "sonstiges" + leere Beschreibung). Caller muss nicht
    selbst behandeln.

    Bei leerer Input-Liste: leere Output-Liste, kein Gemini-Call.
    """
    if not eintraege:
        return []

    prompt = _build_prompt(eintraege)
    try:
        raw_text = await call_gemini(
            prompt,
            temperature=0.2,
            max_output_tokens=4096,
            tenant_id=tenant_id,
            operation_kind="excel_kalkulation_classify",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Gemini-Call fehlgeschlagen, Fallback 'sonstiges': %s", e)
        return _fallback_results(len(eintraege))

    parsed = _parse_gemini_response(raw_text, len(eintraege))
    if parsed is None:
        logger.warning(
            "Gemini-Response nicht parsbar, Fallback 'sonstiges'. "
            "Raw: %r", raw_text[:300],
        )
        return _fallback_results(len(eintraege))

    return [_normalize_one(r) for r in parsed]
