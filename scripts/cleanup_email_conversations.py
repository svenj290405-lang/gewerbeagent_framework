"""
Cleanup-Job: loescht Mail-Konversationen nach Ablauf der Aufbewahrungsfrist.

DSGVO-Datenminimierung: Nach Abwicklung des Termins haben wir keinen
legitimen Grund mehr, Kunden-Mail-Adresse + Name zu speichern.

Zwei Wege in die Loeschung:
  1. ``termin_datum`` liegt vor dem Stichtag — der Vorgang ist durch.
  2. kein Termin, aber der Zustand ist beendet oder ruht seit der vollen
     Frist (storniert, Dialog eingeschlafen, Zustellung gescheitert).

Aufruf:
  Trockenlauf:  uv run python -m scripts.cleanup_email_conversations
  Scharf:       uv run python -m scripts.cleanup_email_conversations --execute
  Eigene Frist: uv run python -m scripts.cleanup_email_conversations --days 30 --execute
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys

from sqlalchemy import delete, select

from core.database import AsyncSessionLocal
from core.models import (
    EmailConversation, STATE_AWAITING_CONFIRMATION, STATE_BOOKED, STATE_CLOSED,
    STATE_DELIVERY_FAILED, STATE_DIALOG, STATE_PROPOSING_SLOTS, STATE_STORNIERT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cleanup")

DEFAULT_RETENTION_DAYS = 14

# Zustaende, in denen eine Konversation ohne Termin nach Ablauf der Frist
# geloescht wird. Bis zum Audit am 2026-08-24 stand hier nur STATE_CLOSED —
# und den setzt genau eine Stelle im Code. Alles, was per Mail storniert
# wurde oder im Dialog stecken blieb, lief damit NIE in eine Frist: live
# lagen Zeilen mit Klarname, Mailadresse und Kundentext 95 Tage jenseits
# der eigenen 90-Tage-Zusage, waehrend der Cron taeglich "0" meldete.
#
# STATE_BOOKED fehlt bewusst: dort steht ein Termin im Kalender. Ist sein
# Datum bekannt, greift der Termin-Zweig; ist es das nicht, waere Loeschen
# ein Datenverlust bei einem laufenden Vorgang.
LOESCHBARE_ZUSTAENDE = (
    STATE_CLOSED, STATE_STORNIERT, STATE_DELIVERY_FAILED,
    STATE_DIALOG, STATE_PROPOSING_SLOTS, STATE_AWAITING_CONFIRMATION,
)


async def cleanup(
    retention_days: int, execute: bool,
    *,
    tenant_id=None,
) -> int:
    """Loescht Konversationen.

    Phase B4: optional tenant_id, dann nur Konversationen DIESES Tenants.
    Default (None) = alle Tenants (Cli-Kompatibilitaet, Backfill-Lauf).
    """
    cutoff = dt.date.today() - dt.timedelta(days=retention_days)
    cutoff_dt = dt.datetime.combine(cutoff, dt.time.min, tzinfo=dt.timezone.utc)

    async with AsyncSessionLocal() as s:
        stmt = select(EmailConversation).where(
            (EmailConversation.termin_datum != None)  # noqa: E711
            & (EmailConversation.termin_datum < cutoff)
        )
        if tenant_id is not None:
            stmt = stmt.where(EmailConversation.tenant_id == tenant_id)
        result = await s.execute(stmt)
        per_termin = list(result.scalars())

        # Ohne Termin entscheidet der Zustand. Bis zum Audit am 2026-08-24
        # stand hier NUR ``STATE_CLOSED`` — und den setzt genau eine Stelle
        # im ganzen Code (document_flow beim Abschluss). Alles, was per Mail
        # storniert wurde oder im Dialog stecken blieb, lief damit nie in
        # eine Frist: live lagen Konversationen mit Klarname, Mailadresse
        # und Kundentext 95 Tage jenseits der eigenen 90-Tage-Zusage, und
        # der Cron meldete dabei taeglich "0 geloescht".
        #
        # Ein Dialog, der seit der vollen Frist ruht, ist kein laufender
        # Vorgang mehr. Bewusst NICHT dabei: STATE_BOOKED ohne
        # ``termin_datum`` — dort steht ein Termin im Kalender, dessen
        # Datum wir nicht kennen; den loescht der Termin-Zweig oben, sobald
        # es gesetzt ist.
        stmt = select(EmailConversation).where(
            (EmailConversation.termin_datum.is_(None))
            & (EmailConversation.state.in_(LOESCHBARE_ZUSTAENDE))
            & (EmailConversation.updated_at < cutoff_dt)
        )
        if tenant_id is not None:
            stmt = stmt.where(EmailConversation.tenant_id == tenant_id)
        result = await s.execute(stmt)
        per_updated = list(result.scalars())

        candidates = {c.id: c for c in per_termin}
        candidates.update({c.id: c for c in per_updated})

        scope = f" tenant={tenant_id}" if tenant_id else ""
        logger.info(
            f"Cutoff-Datum: {cutoff.isoformat()} "
            f"(heute - {retention_days} Tage){scope}"
        )
        logger.info(f"Kandidaten zum Loeschen: {len(candidates)}")

        for c in candidates.values():
            grund = (
                f"termin {c.termin_datum}" if c.termin_datum
                else f"{c.state} seit {c.updated_at.date()}"
            )
            logger.info(
                f"  - {c.kunde_email} (tenant={c.tenant_id}) "
                f"state={c.state} ({grund})"
            )

        if not execute:
            logger.info("Trockenlauf: nichts geloescht. --execute zum scharf laufen.")
            return 0

        if not candidates:
            return 0

        ids = list(candidates.keys())
        result = await s.execute(
            delete(EmailConversation).where(EmailConversation.id.in_(ids))
        )
        await s.commit()
        logger.info(f"Geloescht: {result.rowcount} Konversationen.")
        return result.rowcount


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Loescht alte Mail-Konversationen (DSGVO-Cleanup)."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Aufbewahrungsfrist in Tagen (Default: {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Wirklich loeschen (sonst nur Trockenlauf).",
    )
    args = parser.parse_args()

    deleted = asyncio.run(cleanup(args.days, args.execute))
    return 0 if deleted >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
