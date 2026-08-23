"""DSGVO-Cleanup fuer personenbezogene Daten jenseits der Mail-Konversationen.

scripts/cleanup_email_conversations.py deckt nur EmailConversation ab. Es
gibt aber weitere Tabellen mit personenbezogenen Daten, die bisher NIE
automatisch geloescht wurden (nur via Tenant-Komplettloeschung). Dieses
Modul schliesst die Luecke (Art. 5 Abs. 1 lit. e / Art. 17 DSGVO):

- kundengespraeche  — Anruf-/Gespraechs-Transkripte (raw_transcript,
  notizen_lang, briefing_kurz) + kunde_name. Die Datenschutzerklaerung
  verspricht "Transkripte 90 Tage" — das wird hier technisch durchgesetzt.
- anfrage_tokens (+ anfrage_responses via FK ON DELETE CASCADE) — Web-
  Formular-PII: kunde_email/name/telefon, antworten (JSONB inkl. base64-
  Datei-Uploads), submitted_ip.
- geocode_cache — Kundenadressen → Koordinaten (global, tenant-uebergrei-
  fend gecached).

Geschaeftsunterlagen mit eigener gesetzlicher Aufbewahrungsfrist
(Rechnungen, Angebote — GoBD/HGB) liegen in SEPARATEN Tabellen und werden
hier bewusst NICHT angefasst. Das Loeschen eines Kundengespraechs laesst
ein verknuepftes Angebot unberuehrt (FK angebot_id ist SET NULL auf der
Gespraechs-Seite).

Aufruf (CLI, alle Tenants):
  Trockenlauf:  uv run python -m scripts.cleanup_pii
  Scharf:       uv run python -m scripts.cleanup_pii --execute
  Eigene Frist: uv run python -m scripts.cleanup_pii --days 30 --execute
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys

from sqlalchemy import and_, delete, or_, select, update

from core.database import AsyncSessionLocal
from core.models import (
    AnfrageToken,
    GeocodeCache,
    Kundengespraech,
    Rueckruf,
    Visualisierung,
    WebsiteSalt,
    WebsiteVisit,
    Wissensluecke,
)
from core.models.failed_mail_queue import (
    FAILED_MAIL_DEAD,
    FAILED_MAIL_SENT,
    FailedMailQueue,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cleanup_pii")

DEFAULT_RETENTION_DAYS = 90


def _cutoff(retention_days: int) -> dt.datetime:
    """Aufbewahrungs-Stichtag als tz-aware UTC-datetime."""
    cutoff_date = dt.date.today() - dt.timedelta(days=retention_days)
    return dt.datetime.combine(cutoff_date, dt.time.min, tzinfo=dt.timezone.utc)


async def cleanup_kundengespraeche(
    retention_days: int, execute: bool, *, tenant_id=None,
) -> int:
    """Loescht Gespraechs-Transkripte aelter als retention_days.

    Anker ist gespraech_datum; ein in der Zukunft liegender Termin
    (termin_datum) schuetzt den Eintrag vor Loeschung, damit ein noch
    nicht stattgefundener Termin nicht vorzeitig verschwindet.
    """
    cutoff = _cutoff(retention_days)
    async with AsyncSessionLocal() as s:
        cond = and_(
            Kundengespraech.gespraech_datum < cutoff,
            or_(
                Kundengespraech.termin_datum.is_(None),
                Kundengespraech.termin_datum < cutoff,
            ),
        )
        stmt = select(Kundengespraech.id).where(cond)
        if tenant_id is not None:
            stmt = stmt.where(Kundengespraech.tenant_id == tenant_id)
        ids = list((await s.execute(stmt)).scalars())

        if not execute:
            logger.info(
                f"[kundengespraeche] Trockenlauf: {len(ids)} Kandidaten "
                f"(cutoff={cutoff.date().isoformat()})"
            )
            return 0
        if not ids:
            return 0
        res = await s.execute(
            delete(Kundengespraech).where(Kundengespraech.id.in_(ids))
        )
        await s.commit()
        return res.rowcount


async def cleanup_anfragen(
    retention_days: int, execute: bool, *, tenant_id=None,
) -> int:
    """Loescht Anfrage-Tokens aelter als retention_days.

    anfrage_responses haengen per FK ON DELETE CASCADE dran und werden
    von Postgres mitgeloescht (inkl. base64-Datei-Uploads + submitted_ip).
    Anker ist created_at — deckt auch nie ausgefuellte (abgelaufene)
    Tokens ab.
    """
    cutoff = _cutoff(retention_days)
    async with AsyncSessionLocal() as s:
        stmt = select(AnfrageToken.id).where(AnfrageToken.created_at < cutoff)
        if tenant_id is not None:
            stmt = stmt.where(AnfrageToken.tenant_id == tenant_id)
        ids = list((await s.execute(stmt)).scalars())

        if not execute:
            logger.info(
                f"[anfragen] Trockenlauf: {len(ids)} Kandidaten "
                f"(cutoff={cutoff.date().isoformat()})"
            )
            return 0
        if not ids:
            return 0
        res = await s.execute(
            delete(AnfrageToken).where(AnfrageToken.id.in_(ids))
        )
        await s.commit()
        return res.rowcount


async def cleanup_visualisierungen(
    retention_days: int, execute: bool, *, tenant_id=None,
) -> int:
    """Loescht Foto-Visualisierungen aelter als retention_days.

    Enthaelt kunde_email/kunde_name + Original-/Ergebnis-Foto des Kunden
    (BYTEA) — alles PII. Anker ist created_at. Tabelle hat tenant_id,
    laeuft also pro Tenant wenn tenant_id gesetzt ist.
    """
    cutoff = _cutoff(retention_days)
    async with AsyncSessionLocal() as s:
        stmt = select(Visualisierung.id).where(Visualisierung.created_at < cutoff)
        if tenant_id is not None:
            stmt = stmt.where(Visualisierung.tenant_id == tenant_id)
        ids = list((await s.execute(stmt)).scalars())

        if not execute:
            logger.info(
                f"[visualisierungen] Trockenlauf: {len(ids)} Kandidaten "
                f"(cutoff={cutoff.date().isoformat()})"
            )
            return 0
        if not ids:
            return 0
        res = await s.execute(
            delete(Visualisierung).where(Visualisierung.id.in_(ids))
        )
        await s.commit()
        return res.rowcount


async def cleanup_failed_mail_queue(
    retention_days: int, execute: bool, *, tenant_id=None,
) -> int:
    """Loescht abgeschlossene Mail-Retry-Eintraege aelter als retention_days.

    failed_mail_queue speichert recipient_email + den vollen Mail-Payload
    (html_body, Anhaenge base64) — PII. Nur TERMINALE Eintraege (sent/dead)
    werden geloescht; 'pending' bleibt unberuehrt, damit laufende Retries
    nicht verschwinden. Anker ist created_at.
    """
    cutoff = _cutoff(retention_days)
    async with AsyncSessionLocal() as s:
        cond = and_(
            FailedMailQueue.created_at < cutoff,
            FailedMailQueue.status.in_([FAILED_MAIL_SENT, FAILED_MAIL_DEAD]),
        )
        stmt = select(FailedMailQueue.id).where(cond)
        if tenant_id is not None:
            stmt = stmt.where(FailedMailQueue.tenant_id == tenant_id)
        ids = list((await s.execute(stmt)).scalars())

        if not execute:
            logger.info(
                f"[failed_mail_queue] Trockenlauf: {len(ids)} Kandidaten "
                f"(sent/dead, cutoff={cutoff.date().isoformat()})"
            )
            return 0
        if not ids:
            return 0
        res = await s.execute(
            delete(FailedMailQueue).where(FailedMailQueue.id.in_(ids))
        )
        await s.commit()
        return res.rowcount


async def cleanup_rueckrufe(
    retention_days: int, execute: bool, *, tenant_id=None,
) -> int:
    """Loescht Rueckrufbitten aelter als retention_days.

    Enthalten Name, Telefonnummer und das Anliegen im Freitext — also
    genau die Daten, die ein Anrufer am Telefon hinterlaesst. Die
    Tabelle fehlte bis zum Audit am 2026-08-23 in diesem Skript: die
    Eintraege lagen unbegrenzt herum, obwohl der Betrieb eine
    Aufbewahrungsfrist von 90 Tagen gesetzt hat.

    Anker ist created_at, nicht erledigt_at — sonst wuerde ein nie
    abgehakter Rueckruf ewig bleiben.
    """
    cutoff = _cutoff(retention_days)
    async with AsyncSessionLocal() as s:
        stmt = select(Rueckruf.id).where(Rueckruf.created_at < cutoff)
        if tenant_id is not None:
            stmt = stmt.where(Rueckruf.tenant_id == tenant_id)
        ids = list((await s.execute(stmt)).scalars())

        if not execute:
            logger.info(
                f"[rueckrufe] Trockenlauf: {len(ids)} Kandidaten "
                f"(cutoff={cutoff.date().isoformat()})"
            )
            return 0
        if not ids:
            return 0
        res = await s.execute(delete(Rueckruf).where(Rueckruf.id.in_(ids)))
        await s.commit()
        return res.rowcount


async def cleanup_wissensluecken(
    retention_days: int, execute: bool, *, tenant_id=None,
) -> int:
    """Loescht den Fragesteller aus alten Wissensluecken.

    Die Frage selbst ist Betriebswissen und soll bleiben — sie fuellt
    die Wissensbasis. Der Name des Anrufers oder Mail-Absenders im Feld
    `kunde` hat dagegen nach Ablauf der Frist nichts mehr dort zu
    suchen. Deshalb wird hier nur das Feld geleert, nicht die Zeile
    geloescht.
    """
    cutoff = _cutoff(retention_days)
    async with AsyncSessionLocal() as s:
        stmt = select(Wissensluecke.id).where(
            Wissensluecke.created_at < cutoff,
            Wissensluecke.kunde.isnot(None),
        )
        if tenant_id is not None:
            stmt = stmt.where(Wissensluecke.tenant_id == tenant_id)
        ids = list((await s.execute(stmt)).scalars())

        if not execute:
            logger.info(
                f"[wissensluecken] Trockenlauf: {len(ids)} Kandidaten "
                f"(cutoff={cutoff.date().isoformat()})"
            )
            return 0
        if not ids:
            return 0
        res = await s.execute(
            update(Wissensluecke)
            .where(Wissensluecke.id.in_(ids))
            .values(kunde=None)
        )
        await s.commit()
        return res.rowcount


async def cleanup_geocode_cache(retention_days: int, execute: bool) -> int:
    """Loescht Geocode-Cache-Eintraege (Kundenadressen) aelter als
    retention_days. Tabelle ist tenant-uebergreifend (kein tenant_id) —
    laeuft daher global. Cache-Miss fuehrt beim naechsten Bedarf zu
    Re-Geocoding, kein Datenverlust mit Geschaeftsrelevanz."""
    cutoff = _cutoff(retention_days)
    async with AsyncSessionLocal() as s:
        if not execute:
            cnt = len(list((await s.execute(
                select(GeocodeCache.id).where(GeocodeCache.geocoded_at < cutoff)
            )).scalars()))
            logger.info(
                f"[geocode_cache] Trockenlauf: {cnt} Kandidaten "
                f"(cutoff={cutoff.date().isoformat()})"
            )
            return 0
        res = await s.execute(
            delete(GeocodeCache).where(GeocodeCache.geocoded_at < cutoff)
        )
        await s.commit()
        return res.rowcount


async def cleanup_website_visits(
    retention_days: int = 14, execute: bool = False,
) -> int:
    """Verdichtet Besuchsereignisse zu Tagessummen und loescht die Rohdaten.

    Reihenfolge ist wichtig: erst aggregieren, dann loeschen — sonst
    verschwinden Zahlen. Die Tagessummen bleiben dauerhaft, sie enthalten
    keine Besucherkennung mehr.

    Die Frist ist bewusst 14 Tage: genau so lange, wie die
    Datenschutzerklaerung es fuer Server-Logs zusagt. Der Tages-Salt geht
    schon nach zwei Tagen — danach ist kein Hash mehr nachrechenbar.
    """
    from core.models.website_visit import SALT_TAGE
    from core.services.website_stats import aggregiere_offene_tage

    cutoff_tag = dt.date.today() - dt.timedelta(days=retention_days)
    salt_cutoff = dt.date.today() - dt.timedelta(days=SALT_TAGE)

    async with AsyncSessionLocal() as s:
        alt = list((await s.execute(
            select(WebsiteVisit.id).where(WebsiteVisit.tag < cutoff_tag)
        )).scalars())
        salts = list((await s.execute(
            select(WebsiteSalt.id).where(WebsiteSalt.tag < salt_cutoff)
        )).scalars())

        if not execute:
            logger.info(
                f"[website_visits] Trockenlauf: {len(alt)} Ereignisse, "
                f"{len(salts)} Tagesschluessel (cutoff="
                f"{cutoff_tag.isoformat()})"
            )
            return 0

        # Erst verdichten — auch Tage, die noch nicht faellig sind.
        await aggregiere_offene_tage()

        geloescht = 0
        if alt:
            res = await s.execute(
                delete(WebsiteVisit).where(WebsiteVisit.id.in_(alt))
            )
            geloescht = res.rowcount
        if salts:
            await s.execute(
                delete(WebsiteSalt).where(WebsiteSalt.id.in_(salts))
            )
        await s.commit()
        return geloescht


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Loescht alte PII (Transkripte, Anfragen, Rueckrufe, Visualisierungen, Mail-Queue, Geocode-Cache)."
    )
    parser.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    async def _run() -> None:
        g = await cleanup_kundengespraeche(args.days, args.execute)
        a = await cleanup_anfragen(args.days, args.execute)
        v = await cleanup_visualisierungen(args.days, args.execute)
        f = await cleanup_failed_mail_queue(args.days, args.execute)
        c = await cleanup_geocode_cache(args.days, args.execute)
        r = await cleanup_rueckrufe(args.days, args.execute)
        w = await cleanup_wissensluecken(args.days, args.execute)
        # Feste 14 Tage statt der Tenant-Frist: die Besuchsdaten gehoeren
        # zu keinem Betrieb, sondern zu unserer eigenen Website.
        b = await cleanup_website_visits(execute=args.execute)
        if args.execute:
            logger.info(
                f"Geloescht: {g} Gespraeche, {a} Anfragen, "
                f"{v} Visualisierungen, {f} Mail-Queue-Eintraege, "
                f"{c} Geocode-Eintraege, {r} Rueckrufe, "
                f"{b} Besuchsereignisse; "
                f"{w} Wissensluecken anonymisiert."
            )

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
