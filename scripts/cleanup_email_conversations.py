"""
Cleanup-Job: loescht Mail-Konversationen deren Termin > 14 Tage zurueckliegt.

DSGVO-Datenminimierung: Nach Abwicklung des Termins haben wir keinen
legitimen Grund mehr, Kunden-Mail-Adresse + Name zu speichern.

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
from core.models import EmailConversation, STATE_CLOSED

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cleanup")

DEFAULT_RETENTION_DAYS = 14


async def cleanup(retention_days: int, execute: bool) -> int:
    cutoff = dt.date.today() - dt.timedelta(days=retention_days)
    cutoff_dt = dt.datetime.combine(cutoff, dt.time.min, tzinfo=dt.timezone.utc)

    async with AsyncSessionLocal() as s:
        result = await s.execute(
            select(EmailConversation).where(
                (EmailConversation.termin_datum != None)  # noqa: E711
                & (EmailConversation.termin_datum < cutoff)
            )
        )
        per_termin = list(result.scalars())

        result = await s.execute(
            select(EmailConversation).where(
                (EmailConversation.termin_datum.is_(None))
                & (EmailConversation.state == STATE_CLOSED)
                & (EmailConversation.updated_at < cutoff_dt)
            )
        )
        per_updated = list(result.scalars())

        candidates = {c.id: c for c in per_termin}
        candidates.update({c.id: c for c in per_updated})

        logger.info(
            f"Cutoff-Datum: {cutoff.isoformat()} (heute - {retention_days} Tage)"
        )
        logger.info(f"Kandidaten zum Loeschen: {len(candidates)}")

        for c in candidates.values():
            grund = (
                f"termin {c.termin_datum}" if c.termin_datum
                else f"closed seit {c.updated_at.date()}"
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
