"""Merge-Skript Kundendatenbank Phase 8 — Kunde B geht in Kunde A auf.

Duenner CLI-Wrapper um core/services/kunde_merge.py (dieselbe Logik
nutzt der App-Endpoint POST /app/api/kunden/merge). Fuer die manuelle
Aufloesung von needs_review-Faellen und die Nacharbeit nach dem
Lexware-Import: ein Mensch hat entschieden, dass zwei Kunden-Zeilen
dieselbe Person sind.

Regeln (Details im Service-Docstring): FKs aller acht Bestandstabellen
umhaengen, External-Refs verschieben, fehlende Merkmale uebernehmen,
Quelle additive-only per merged_into_id stilllegen, Ketten einstufig.
Abbruch ohne Aenderung bei Ref-Konflikt im selben System,
Tenant-Grenze, Zyklus oder anderweitig gemergter Quelle.

needs_review wird an der Quelle geloescht (sie ist ab jetzt aus allen
Lookups raus); am Ziel nur mit --clear-review, weil das Ziel noch mit
einem dritten Kunden mehrdeutig sein kann.

Aufruf (im Container):
  Trockenlauf:   uv run python -m scripts.merge_kunden <quelle-id> <ziel-id>
  Scharf:        uv run python -m scripts.merge_kunden <quelle-id> <ziel-id> --execute
  + Review weg:  ... --execute --clear-review
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from core.database import AsyncSessionLocal
from core.models import Kunde
from core.services.kunde_merge import MergeAbbruch, merge_kunden


def _kunde_zeile(k: Kunde) -> str:
    merkmale = ", ".join(
        f"{feld}={wert!r}" for feld, wert in (
            ("email", k.email), ("telefon", k.telefon),
            ("adresse", k.adresse),
        ) if wert
    )
    review = " [needs_review]" if k.needs_review else ""
    return f"{k.id} {k.name!r} ({k.identity_key}){review} {merkmale}".rstrip()


async def merge(
    quelle_id: uuid.UUID,
    ziel_id: uuid.UUID,
    *,
    execute: bool,
    clear_review: bool,
) -> None:
    async with AsyncSessionLocal() as s:
        e = await merge_kunden(
            s, quelle_id, ziel_id, clear_review=clear_review,
        )

        if e.ziel_war_gemergt:
            print("Hinweis: Ziel war selbst gemergt — finale Ziel-Zeile "
                  "wurde verwendet.")
        if e.schon_gemergt:
            print("Quelle ist bereits in dieses Ziel gemergt — "
                  "nichts zu tun.")
            return

        print(f"Quelle: {_kunde_zeile(e.quelle)}")
        print(f"Ziel:   {_kunde_zeile(e.ziel)}")
        for tabelle, anzahl in e.umgehaengt.items():
            print(f"  {tabelle}: {anzahl} Zeile(n) umgehaengt")
        for ref in e.refs_verschoben:
            print(f"  kunde_external_ref: {ref} ans Ziel verschoben")
        for feld in e.merkmale_uebernommen:
            print(f"  {feld} uebernommen: {getattr(e.ziel, feld)!r}")
        if e.ketten_umgehaengt:
            print(f"  {e.ketten_umgehaengt} bereits gemergte(r) Kunde(n) "
                  "direkt aufs neue Ziel umgehaengt")
        if clear_review:
            print("  needs_review am Ziel entfernt (--clear-review)")
        elif e.ziel.needs_review:
            print("  Hinweis: Ziel behaelt needs_review (ggf. weitere "
                  "Mehrdeutigkeit; --clear-review zum Entfernen)")
        if len(e.drive_keys) > 1:
            print(f"  Hinweis: Ziel hat jetzt {len(e.drive_keys)} "
                  "Drive-Ordner-Zuordnungen (Keys: "
                  + ", ".join(e.drive_keys)
                  + ") — Dateien ggf. manuell in einen Ordner ziehen.")

        if execute:
            await s.commit()
            print("\nMerge geschrieben.")
        else:
            await s.rollback()
            print("\nTROCKENLAUF — nichts geschrieben "
                  "(--execute zum Ausfuehren).")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kundendatenbank Phase 8: Kunde B (Quelle) in "
                    "Kunde A (Ziel) aufgehen lassen "
                    "(Trockenlauf ohne --execute)",
    )
    parser.add_argument("quelle", type=uuid.UUID,
                        help="Kunden-ID der Quelle (geht im Ziel auf)")
    parser.add_argument("ziel", type=uuid.UUID,
                        help="Kunden-ID des Ziels (bleibt bestehen)")
    parser.add_argument("--execute", action="store_true",
                        help="Aenderungen wirklich schreiben "
                             "(sonst Trockenlauf)")
    parser.add_argument("--clear-review", action="store_true",
                        help="needs_review auch am Ziel entfernen")
    args = parser.parse_args()

    try:
        asyncio.run(merge(
            args.quelle, args.ziel,
            execute=args.execute, clear_review=args.clear_review,
        ))
    except MergeAbbruch as e:
        print(f"ABBRUCH: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
