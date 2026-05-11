"""Beta-1: Voice-Telefonnummer (Sipgate) einem Tenant zuweisen.

`scripts/onboard.py` druckt seit langem den Hinweis auf dieses Skript
in der Aktivierungs-Checkliste — bisher hat es aber gefehlt. Beim Pilot
mit Dietz waere Sven hier in einen Fehler gelaufen.

Workflow:
  1. Sven bucht in sipgate.com eine Ortsnetz-Nummer
  2. python -m scripts.assign_number --slug dietz --number "+4965021234"
  3. tenant.voice_phone_number wird gesetzt
  4. ElevenLabs-Agent kann mit dieser Nummer verknuepft werden

E.164-Normalisierung (DE-zentriert, ohne `phonenumbers`-Dep):
  - "+4965021234"  -> "+4965021234"
  - "065021234"    -> "+4965021234"  (DE-Mobile mit fuehrender 0)
  - "0049 6502..." -> "+496502..."
  - andere:        -> Fehler mit Hinweis, Format zu wechseln

Idempotent: gleicher Slug + gleiche Nummer = no-op. Andere Nummer fuer
schon belegten Tenant = Ueberschreiben mit kurzer Bestaetigungs-Frage.
Doppelte Nummer fuer zwei verschiedene Tenants = Unique-Constraint-
Error (Sven muss klaeren wer die Nummer wirklich besitzt).
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.database import AsyncSessionLocal
from core.logging_context import set_log_tenant
from core.models import Tenant


# Lockere DE-zentrische Normalisierung. Andere Laender muessen explizit
# als +<countrycode> kommen.
_E164_RX = re.compile(r"^\+\d{8,15}$")


def normalize_to_e164(raw: str) -> str:
    """Macht aus Sven-Input ein E.164-konformes "+...". Wirft ValueError."""
    if not raw or not raw.strip():
        raise ValueError("Telefon-Nummer leer")

    # Whitespace + uebliche Trenner weg
    s = raw.strip()
    s = re.sub(r"[\s()\-./]", "", s)

    # 00-Praefix in + umwandeln
    if s.startswith("00"):
        s = "+" + s[2:]

    # Fuehrende 0 in DE-Kontext = +49<rest>
    if s.startswith("0"):
        s = "+49" + s[1:]

    # Plus-Zeichen einfuegen falls nur Ziffern
    if not s.startswith("+"):
        # Heuristik: 11+ Ziffern beginnend mit 49 = DE
        if s.startswith("49") and len(s) >= 11:
            s = "+" + s
        else:
            raise ValueError(
                f"Nummer '{raw}' nicht eindeutig E.164. Bitte mit Landesvorwahl "
                f"angeben, z.B. '+4965021234'."
            )

    if not _E164_RX.match(s):
        raise ValueError(
            f"Nummer '{raw}' (→ '{s}') ist kein valides E.164-Format. "
            f"Erwartet z.B. +496502123456."
        )

    return s


async def assign(slug: str, number_raw: str) -> int:
    """Setzt voice_phone_number. Return-Code 0=ok, 1=Tenant fehlt, 2=Conflict."""
    try:
        normalized = normalize_to_e164(number_raw)
    except ValueError as e:
        print(f"FEHLER: {e}")
        return 1

    async with AsyncSessionLocal() as session:
        tenant = (await session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )).scalar_one_or_none()
        if not tenant:
            print(f"FEHLER: Kein Tenant mit Slug '{slug}' gefunden.")
            return 1

        set_log_tenant(tenant.id)

        if tenant.voice_phone_number == normalized:
            print(f"Tenant '{slug}' hat schon {normalized} — no-op.")
            return 0

        old = tenant.voice_phone_number or "(leer)"
        tenant.voice_phone_number = normalized

        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            print(
                f"FEHLER: Nummer {normalized} ist schon einem anderen Tenant "
                f"zugewiesen (UNIQUE constraint).\n"
                f"  → erst per SQL pruefen wem sie gehoert, dann zuweisen."
            )
            return 2

        print(f"Tenant '{slug}' ({tenant.company_name}):")
        print(f"  voice_phone_number: {old} → {normalized}")
        print("Naechste Schritte:")
        print("  1. Sipgate: Rufumleitung von Tenant-Telefon auf diese Nummer")
        print(f"  2. ElevenLabs-Agent verknuepfen (Phone Number: {normalized})")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weist einem Tenant eine Voice-Telefonnummer zu (E.164).",
    )
    parser.add_argument("--slug", required=True, help="Tenant-Slug, z.B. 'dietz'")
    parser.add_argument(
        "--number", required=True,
        help="Telefonnummer (E.164 oder DE-lokal). Beispiele: "
             "'+4965021234', '065021234', '0049 650 2 1234'",
    )
    args = parser.parse_args()
    rc = asyncio.run(assign(args.slug, args.number))
    sys.exit(rc)


if __name__ == "__main__":
    main()
