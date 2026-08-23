"""Vergleicht die ORM-Modelle mit dem echten DB-Schema (read-only).

Warum es das braucht: `core.database.base.Base` haengt jeder Tabelle
automatisch `created_at`/`updated_at` an. Wer eine Migration von Hand
schreibt und das vergisst, bekommt eine Tabelle, die das Modell nicht
mehr bedienen kann — der Fehler faellt erst auf, wenn irgendwann der
erste Voll-ORM-Zugriff passiert (bei `geocode_cache` bis heute nicht,
weil Geocoding aus ist). Genau dieser Fall trat schon einmal bei
`health_check_results` auf und wurde per Nachtrags-Migration geheilt.

Meldet:
  * Tabellen, die das Modell kennt, die DB aber nicht
  * Spalten, die das Modell erwartet und die DB nicht hat  (gefaehrlich)
  * Spalten, die die DB hat und das Modell nicht kennt      (Hinweis)
  * NOT-NULL im Modell, aber NULL-bar in der DB             (Hinweis)

Exit-Code 1, sobald eine erwartete Spalte oder Tabelle fehlt.

  docker compose exec -T -e PYTHONPATH=/app framework \\
      .venv/bin/python scripts/schema_drift_check.py
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from core.database import AsyncSessionLocal
from core.database.base import Base
import core.models  # noqa: F401  — registriert alle Modelle an Base.metadata


async def main() -> int:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT table_name, column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        ))).all()

    db: dict[str, dict[str, bool]] = {}
    for tabelle, spalte, nullable in rows:
        db.setdefault(tabelle, {})[spalte] = (nullable == "YES")

    fehlende_tabellen: list[str] = []
    fehlende_spalten: list[str] = []
    unbekannte_spalten: list[str] = []
    null_abweichung: list[str] = []

    for name, tabelle in sorted(Base.metadata.tables.items()):
        if name not in db:
            fehlende_tabellen.append(name)
            continue
        ist = db[name]
        for spalte in tabelle.columns:
            if spalte.name not in ist:
                fehlende_spalten.append(f"{name}.{spalte.name}")
            elif not spalte.nullable and ist[spalte.name]:
                null_abweichung.append(f"{name}.{spalte.name}")
        modell_spalten = {c.name for c in tabelle.columns}
        for spalte in ist:
            if spalte not in modell_spalten:
                unbekannte_spalten.append(f"{name}.{spalte}")

    print("=" * 70)
    print(f"Schema-Abgleich: {len(Base.metadata.tables)} Modell-Tabellen, "
          f"{len(db)} DB-Tabellen")
    print("=" * 70)

    def zeige(titel: str, eintraege: list[str], schwer: bool) -> None:
        marke = "❌" if schwer else "ℹ️ "
        if not eintraege:
            print(f"✅ {titel}: keine")
            return
        print(f"{marke} {titel}: {len(eintraege)}")
        for e in eintraege:
            print(f"     {e}")

    zeige("Tabellen fehlen in der DB", fehlende_tabellen, True)
    zeige("Spalten fehlen in der DB (Modell erwartet sie)",
          fehlende_spalten, True)
    zeige("NOT NULL im Modell, aber NULL-bar in der DB",
          null_abweichung, False)
    zeige("Spalten nur in der DB (Modell kennt sie nicht)",
          unbekannte_spalten, False)

    print("=" * 70)
    return 1 if (fehlende_tabellen or fehlende_spalten) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
