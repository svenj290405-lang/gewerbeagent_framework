"""
Kalkulation: Sicheres Auswerten von Handwerker-Formeln.

Wird verwendet, um die in `tenant_kalkulationen` hinterlegten Formeln auf
Variablen-Werte (von Gemini extrahiert) deterministisch in Python anzuwenden.

Sicherheits-Prinzip: Wir parsen die Formel mit `ast.parse(mode="eval")`
und whitelisten *einzelne* AST-Knoten. Damit ist
  - kein Attribut-Zugriff (`x.__class__`)
  - kein Funktionsaufruf ausser einem fest definierten Set
  - kein Import / kein Subscript / keine Walrus / keine F-Strings
moeglich. Selbst wenn der Handwerker (oder ein Excel-Sheet) `os.system(...)`
hineinschreibt, schlaegt das Parsen fehl.

Erlaubte Operationen:
  - Arithmetik: +, -, *, /, //, %, **
  - Unaere: -x, +x
  - Vergleich + Bool fuer Bedingungen: ==, !=, <, <=, >, >=, and, or, not
  - Conditional: x if bedingung else y
  - Funktionen: min, max, round, abs, ceil, floor, int, float

Variablen-Namen: snake_case, [a-zA-Z_][a-zA-Z0-9_]*
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Funktionen, die in Formeln aufgerufen werden duerfen. Bewusst minimal.
SAFE_FUNCTIONS: dict[str, object] = {
    "min": min,
    "max": max,
    "round": round,
    "abs": abs,
    "ceil": math.ceil,
    "floor": math.floor,
    "int": int,
    "float": float,
}


# Reservierte Namen, die NICHT als Variable verwendet werden duerfen
# (Funktionen, Konstanten). Verhindert Verwirrung wie "min = 5; min(min,3)".
RESERVED_NAMES = set(SAFE_FUNCTIONS.keys()) | {"True", "False", "None"}


_VAR_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class FormelResult:
    wert: float
    formel: str
    variablen: dict[str, float]


class FormelError(ValueError):
    """Formel ist syntaktisch oder logisch ungueltig."""


# ---------------------------------------------------------------------------
# AST-Validator
# ---------------------------------------------------------------------------


# Erlaubte AST-Node-Typen. Alles, was nicht hier steht, fliegt raus.
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    # Literale
    ast.Constant,
    ast.Num,  # py<3.12 backcompat (deprecated, aber harmlos)
    # Variablen / Funktionsnamen
    ast.Name,
    ast.Load,
    # Operationen
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    # Operatoren
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub,
    ast.And, ast.Or, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    # Funktionsaufruf
    ast.Call,
)


def _validate(node: ast.AST) -> None:
    """Recursive Whitelist-Pruefung. Wirft FormelError bei Verstoss."""
    if not isinstance(node, _ALLOWED_NODES):
        raise FormelError(
            f"Nicht erlaubter Ausdruck: {type(node).__name__}"
        )

    # Calls duerfen nur SAFE_FUNCTIONS aufrufen, nichts mit Attribut.
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise FormelError("Nur einfache Funktionsaufrufe erlaubt.")
        if node.func.id not in SAFE_FUNCTIONS:
            raise FormelError(
                f"Funktion '{node.func.id}' nicht erlaubt. "
                f"Verfuegbar: {', '.join(sorted(SAFE_FUNCTIONS))}"
            )
        if node.keywords:
            raise FormelError("Keyword-Argumente sind nicht erlaubt.")

    # Constants duerfen nur Zahlen / bool sein.
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, bool)):
            raise FormelError(
                f"Nur Zahlen / True / False erlaubt, nicht "
                f"{type(node.value).__name__}"
            )

    for child in ast.iter_child_nodes(node):
        _validate(child)


# ---------------------------------------------------------------------------
# Variablen-Extraktion
# ---------------------------------------------------------------------------


def parse_variables(formel: str) -> list[str]:
    """
    Liefert die Variablennamen, die in `formel` verwendet werden, in
    stabiler Reihenfolge (erstes Vorkommen). Funktionsnamen sind
    ausgenommen. Wirft FormelError bei Syntax-/Sicherheits-Fehlern.
    """
    formel = (formel or "").strip()
    if not formel:
        raise FormelError("Formel ist leer.")

    try:
        tree = ast.parse(formel, mode="eval")
    except SyntaxError as exc:
        raise FormelError(f"Syntax-Fehler: {exc.msg}") from exc

    _validate(tree)

    # Funktionsnamen aus den Calls einsammeln, damit wir sie nicht als
    # Variablen ausweisen.
    func_names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            func_names.add(n.func.id)

    seen: list[str] = []
    seen_set: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id not in func_names:
            if n.id in RESERVED_NAMES:
                # 'min' / 'max' / ... darf nicht als Variable benutzt werden
                raise FormelError(
                    f"'{n.id}' ist reserviert und kann keine Variable sein."
                )
            if not _VAR_RE.match(n.id):
                raise FormelError(
                    f"'{n.id}' ist kein gueltiger Variablen-Name "
                    "(snake_case, Buchstaben/Zahlen/Unterstrich)."
                )
            if n.id not in seen_set:
                seen.append(n.id)
                seen_set.add(n.id)
    return seen


# ---------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------


def safe_eval_formel(
    formel: str,
    variablen: dict[str, float | int],
) -> float:
    """
    Wertet `formel` mit den gegebenen Variablen-Werten aus.

    - Validiert Formel via `parse_variables` (wirft FormelError bei
      verbotenen Konstrukten).
    - Pruef alle Variablen auf Anwesenheit + numerischen Typ.
    - Eval mit eingeschraenktem `__builtins__`.

    Returns: numerischer Wert (immer float).
    """
    needed = parse_variables(formel)

    # Pruefen, dass alle benoetigten Variablen geliefert wurden
    fehlend = [v for v in needed if v not in variablen]
    if fehlend:
        raise FormelError(
            "Es fehlen Variablen: " + ", ".join(fehlend)
        )

    # Werte normalisieren auf float
    vars_clean: dict[str, float] = {}
    for name in needed:
        raw = variablen[name]
        if isinstance(raw, bool):
            # bool ist subclass von int - aber wir wollen Zahlen
            vars_clean[name] = float(int(raw))
        elif isinstance(raw, (int, float)):
            vars_clean[name] = float(raw)
        else:
            try:
                vars_clean[name] = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise FormelError(
                    f"Variable '{name}' = {raw!r} ist nicht numerisch."
                ) from exc

    namespace = {**SAFE_FUNCTIONS, **vars_clean}

    try:
        # __builtins__ ausschalten, damit auch im Edge-Case nichts geht
        result = eval(  # noqa: S307 (validiert via AST)
            compile(formel, "<formel>", "eval"),
            {"__builtins__": {}},
            namespace,
        )
    except FormelError:
        raise
    except ZeroDivisionError as exc:
        raise FormelError("Division durch Null.") from exc
    except Exception as exc:  # noqa: BLE001 (defensive)
        raise FormelError(f"Auswertungs-Fehler: {exc}") from exc

    if isinstance(result, bool):
        return float(int(result))
    if not isinstance(result, (int, float)):
        raise FormelError(
            f"Formel-Ergebnis ist kein Zahl ({type(result).__name__})."
        )
    if math.isnan(result) or math.isinf(result):
        raise FormelError("Ergebnis ist NaN / Infinity.")
    return float(result)


def compute_kalkulation(
    formel: str,
    variablen: dict[str, float | int],
) -> FormelResult:
    """High-level Wrapper - liefert FormelResult fuer Logging/Preview."""
    wert = safe_eval_formel(formel, variablen)
    return FormelResult(
        wert=wert,
        formel=formel,
        variablen={k: float(v) for k, v in variablen.items()},
    )
