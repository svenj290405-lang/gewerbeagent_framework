"""Ueberschlags-Rechnung: Formeln des Betriebs deterministisch auswerten.

Warum ueberhaupt: "Was kostet das ungefaehr?" ist die haeufigste Frage am
Telefon. Bisher konnte Q darauf nur ausweichen. Ein Sprachmodell rechnen
zu lassen waere die falsche Antwort — es wuerde plausibel klingende
Zahlen erfinden, und der Kunde haelt sie fuer verbindlich.

Deshalb Hybrid:
  * Der Betrieb schreibt die Formel auf   -> ``TenantKalkulation.formel``
  * Q sammelt nur die Variablen beim Kunden ein
  * Gerechnet wird hier, in Python, nachvollziehbar

Der Auswerter ist ein AST-Walker mit Whitelist statt ``eval()`` oder einer
Zusatz-Abhaengigkeit. Erlaubt sind Zahlen, Variablen, + - * / // % **,
Klammern, unaeres Minus und die Funktionen min/max/round/abs. Alles
andere — Attribute, Aufrufe, Indizes, Namen ausserhalb der Variablenliste
— fliegt raus. Damit ist eine Formel selbst dann harmlos, wenn sie ueber
ein Q-Tool aus einem Gespraech heraus angelegt wurde.
"""
from __future__ import annotations

import ast
import logging
import uuid

from sqlalchemy import select

from core.database.connection import get_session
from core.models.tenant_kalkulation import TenantKalkulation

logger = logging.getLogger(__name__)


class FormelFehler(ValueError):
    """Formel ist ungueltig oder nicht auswertbar."""


# Nur diese Knotentypen duerfen in einer Formel vorkommen.
_ERLAUBTE_OPS = (
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
)
_ERLAUBTE_FUNKTIONEN = {
    "min": min, "max": max, "round": round, "abs": abs,
}

# Schutz gegen 2**999999999 — eine Formel darf den Prozess nicht aufhaengen.
MAX_EXPONENT = 8
MAX_FORMEL_LEN = 1000


def variablen_aus_formel(formel: str) -> list[str]:
    """Alle Variablennamen einer Formel, in Reihenfolge des Auftretens.

    Damit muss der Betrieb die Variablenliste nicht doppelt pflegen —
    sie faellt aus der Formel selbst ab.
    """
    try:
        baum = ast.parse(formel or "", mode="eval")
    except SyntaxError as exc:
        raise FormelFehler(f"Formel ist nicht lesbar: {exc.msg}") from exc
    # ast.walk laeuft breitenweise — fuer die Reihenfolge, in der Q die
    # Werte erfragen soll, zaehlt aber die Position im Quelltext.
    gefunden: list[tuple[int, int, str]] = []
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Name) and knoten.id not in _ERLAUBTE_FUNKTIONEN:
            gefunden.append((knoten.lineno, knoten.col_offset, knoten.id))
    gefunden.sort()
    namen: list[str] = []
    for _, _, name in gefunden:
        if name not in namen:
            namen.append(name)
    return namen


def pruefe_formel(formel: str) -> list[str]:
    """Validiert eine Formel und gibt ihre Variablen zurueck.

    Wirft ``FormelFehler`` mit einer Meldung, die man einem Handwerker
    zeigen kann — nicht mit einem Python-Traceback.
    """
    formel = (formel or "").strip()
    if not formel:
        raise FormelFehler("Die Formel ist leer.")
    if len(formel) > MAX_FORMEL_LEN:
        raise FormelFehler("Die Formel ist zu lang.")
    namen = variablen_aus_formel(formel)
    # Testlauf mit 1 fuer jede Variable — faengt verbotene Konstrukte und
    # Unsinn wie "qm * " sofort, statt erst beim Kundengespraech.
    try:
        _auswerten(formel, {n: 1.0 for n in namen})
    except ZeroDivisionError:
        pass  # bei echten Werten kein Problem
    return namen


def _auswerten(formel: str, werte: dict[str, float]) -> float:
    baum = ast.parse(formel, mode="eval")

    def gehe(knoten):
        if isinstance(knoten, ast.Expression):
            return gehe(knoten.body)
        if isinstance(knoten, ast.Constant):
            if isinstance(knoten.value, bool) or not isinstance(
                knoten.value, (int, float)
            ):
                raise FormelFehler("Nur Zahlen sind erlaubt.")
            return float(knoten.value)
        if isinstance(knoten, ast.Name):
            if knoten.id not in werte:
                raise FormelFehler(f"Es fehlt ein Wert fuer '{knoten.id}'.")
            return float(werte[knoten.id])
        if isinstance(knoten, ast.UnaryOp):
            if not isinstance(knoten.op, _ERLAUBTE_OPS):
                raise FormelFehler("Dieses Rechenzeichen ist nicht erlaubt.")
            wert = gehe(knoten.operand)
            return -wert if isinstance(knoten.op, ast.USub) else wert
        if isinstance(knoten, ast.BinOp):
            if not isinstance(knoten.op, _ERLAUBTE_OPS):
                raise FormelFehler("Dieses Rechenzeichen ist nicht erlaubt.")
            links, rechts = gehe(knoten.left), gehe(knoten.right)
            if isinstance(knoten.op, ast.Pow):
                if abs(rechts) > MAX_EXPONENT:
                    raise FormelFehler("Der Exponent ist zu gross.")
                return links ** rechts
            if isinstance(knoten.op, ast.Add):
                return links + rechts
            if isinstance(knoten.op, ast.Sub):
                return links - rechts
            if isinstance(knoten.op, ast.Mult):
                return links * rechts
            if isinstance(knoten.op, ast.Div):
                return links / rechts
            if isinstance(knoten.op, ast.FloorDiv):
                return links // rechts
            if isinstance(knoten.op, ast.Mod):
                return links % rechts
        if isinstance(knoten, ast.Call):
            if not isinstance(knoten.func, ast.Name):
                raise FormelFehler("Dieser Aufruf ist nicht erlaubt.")
            fn = _ERLAUBTE_FUNKTIONEN.get(knoten.func.id)
            if fn is None or knoten.keywords:
                raise FormelFehler(
                    f"Unbekannte Funktion '{getattr(knoten.func, 'id', '?')}'. "
                    f"Erlaubt: {', '.join(sorted(_ERLAUBTE_FUNKTIONEN))}."
                )
            return float(fn(*[gehe(a) for a in knoten.args]))
        raise FormelFehler("Die Formel enthaelt etwas, das nicht erlaubt ist.")

    ergebnis = gehe(baum)
    if ergebnis != ergebnis or ergebnis in (float("inf"), float("-inf")):
        raise FormelFehler("Das Ergebnis ist keine gueltige Zahl.")
    return float(ergebnis)


async def rechne(
    tenant_id: uuid.UUID, name_oder_id: str, werte: dict[str, float],
) -> dict:
    """Wertet die benannte Formel eines Tenants aus.

    Rueckgabe enthaelt bewusst auch ``formel`` und ``werte`` — damit die
    Zahl im Gespraech, in der App und im Log nachvollziehbar ist und nicht
    als Orakel dasteht.
    """
    such = (name_oder_id or "").strip().lower()
    async with get_session() as s:
        rows = (await s.execute(
            select(TenantKalkulation)
            .where(TenantKalkulation.tenant_id == tenant_id)
            .where(TenantKalkulation.aktiv.is_(True))
            .order_by(TenantKalkulation.sortierung, TenantKalkulation.name)
        )).scalars().all()

    treffer = None
    for r in rows:
        if str(r.id) == such or r.name.lower() == such:
            treffer = r
            break
    if treffer is None:
        for r in rows:
            if such and such in r.name.lower():
                treffer = r
                break
    if treffer is None:
        return {
            "ok": False,
            "error": f"Keine Formel zu '{name_oder_id}' gefunden.",
            "verfuegbar": [r.name for r in rows],
        }

    benoetigt = treffer.variablen or variablen_aus_formel(treffer.formel)
    sauber: dict[str, float] = {}
    fehlend: list[str] = []
    for v in benoetigt:
        roh = werte.get(v)
        if roh is None or str(roh).strip() == "":
            fehlend.append(v)
            continue
        try:
            sauber[v] = float(str(roh).replace(",", "."))
        except (TypeError, ValueError):
            fehlend.append(v)
    if fehlend:
        return {
            "ok": False,
            "error": "Es fehlen Angaben: " + ", ".join(fehlend),
            "fehlend": fehlend,
            "name": treffer.name,
        }

    try:
        ergebnis = _auswerten(treffer.formel, sauber)
    except FormelFehler as exc:
        logger.warning(
            "Kalkulation %s (tenant=%s) fehlerhaft: %s", treffer.name, tenant_id, exc
        )
        return {"ok": False, "error": str(exc), "name": treffer.name}
    except ZeroDivisionError:
        return {
            "ok": False,
            "error": "Bei diesen Werten wird durch null geteilt.",
            "name": treffer.name,
        }

    return {
        "ok": True,
        "name": treffer.name,
        "ergebnis": round(ergebnis, 2),
        "einheit": treffer.einheit or "EUR",
        "formel": treffer.formel,
        "werte": sauber,
        "text": ueberschlag_text(
            treffer.name, ergebnis, treffer.einheit or "EUR"
        ),
    }


def ueberschlag_text(name: str, ergebnis: float, einheit: str = "EUR") -> str:
    """Formulierung, die klarstellt: Richtwert, kein Angebot.

    Steht hier und nicht im Prompt, damit die Einordnung auf jedem Kanal
    identisch ist — am Telefon, in der Mail und in der App.
    """
    betrag = f"{ergebnis:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
    return (
        f"{name}: ueberschlaegig rund {betrag} {einheit}. "
        "Das ist ein Richtwert nach den hinterlegten Ansaetzen und kein "
        "verbindliches Angebot — das macht der Betrieb nach Sichtung."
    )
