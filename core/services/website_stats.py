"""Auswertung der Website-Besuche — Aggregation und Kennzahlen.

Zwei Aufgaben:

1. **Verdichten**: aus den Rohereignissen eines Tages werden Tagessummen
   (`website_tage`). Danach koennen die Rohzeilen weg, ohne dass Zahlen
   verlorengehen — die Summen enthalten keine Besucherkennung mehr.
2. **Auswerten**: die Zahlen fuer die Akquise-Seite im Admin.

Bewusst ein eigener Service statt weiterer Zeilen in
`core/admin/routes.py` — die Datei ist mit ~1700 Zeilen am Limit. Die
BESTEHENDEN Auswertungen dort bleiben unangetastet: sie funktionieren,
haben aber kein Testnetz, und ein Umbau waere Risiko ohne Gegenwert.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import Date, cast, distinct, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert

from core.database import AsyncSessionLocal
from core.models.website_visit import (
    ART_AUFRUF, ART_KONTAKT, WebsiteTag, WebsiteVisit, heute_lokal,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Verdichten
# ---------------------------------------------------------------------

async def aggregiere_tag(tag: dt.date) -> int:
    """Schreibt die Tagessummen eines Tages. Wiederholbar.

    Zaehlt neu und ueberschreibt — ein zweiter Lauf fuer denselben Tag
    ergibt dieselben Zahlen, kein Aufaddieren. Das ist wichtig, weil der
    naechtliche Aufraeumer denselben Tag mehrfach sehen kann.
    """
    async with AsyncSessionLocal() as s:
        zeilen = (await s.execute(
            # literal_column statt "" als Parameter: sonst setzt SQLAlchemy
            # in SELECT und GROUP BY zwei verschiedene Platzhalter ein und
            # Postgres erkennt den Ausdruck nicht wieder ("must appear in
            # the GROUP BY clause").
            select(
                WebsiteVisit.pfad,
                func.coalesce(
                    WebsiteVisit.ref_host, literal_column("''"),
                ).label("ref_host"),
                WebsiteVisit.art,
                func.count().label("aufrufe"),
                func.count(distinct(WebsiteVisit.besucher_hash)).label("besucher"),
            )
            .where(WebsiteVisit.tag == tag)
            .where(WebsiteVisit.bot.is_(False))
            .group_by(WebsiteVisit.pfad,
                      func.coalesce(WebsiteVisit.ref_host, literal_column("''")),
                      WebsiteVisit.art)
        )).all()

        for pfad, ref_host, art, aufrufe, besucher in zeilen:
            await s.execute(
                insert(WebsiteTag.__table__)
                .values(tag=tag, pfad=pfad, ref_host=ref_host, art=art,
                        aufrufe=aufrufe, besucher=besucher)
                .on_conflict_do_update(
                    constraint="uq_website_tage",
                    set_={"aufrufe": aufrufe, "besucher": besucher},
                )
            )
        await s.commit()
    return len(zeilen)


async def aggregiere_offene_tage(bis_einschliesslich: dt.date | None = None) -> int:
    """Verdichtet alle Tage mit Rohdaten bis zum Stichtag (ohne heute).

    Der laufende Tag bleibt aussen vor — er waechst noch, und eine
    Tagessumme, die sich noch aendert, verwirrt mehr als sie nutzt.
    """
    grenze = bis_einschliesslich or (heute_lokal() - dt.timedelta(days=1))
    async with AsyncSessionLocal() as s:
        tage = list((await s.execute(
            select(distinct(WebsiteVisit.tag))
            .where(WebsiteVisit.tag <= grenze)
            .order_by(WebsiteVisit.tag)
        )).scalars())
    for tag in tage:
        await aggregiere_tag(tag)
    return len(tage)


# ---------------------------------------------------------------------
# Auswerten
# ---------------------------------------------------------------------

async def kennzahlen(tage: int = 30) -> dict:
    """Kacheln fuer die Akquise-Seite.

    Mischt bewusst zwei Quellen: der laufende Tag kommt aus den
    Rohdaten (dort steht er sofort), aeltere Tage aus den Summen (die
    bleiben auch nach dem Loeschen der Rohdaten erhalten).
    """
    heute = heute_lokal()
    von = heute - dt.timedelta(days=max(1, tage) - 1)

    async with AsyncSessionLocal() as s:
        # Heute: direkt aus den Rohdaten.
        heute_zeile = (await s.execute(
            select(
                func.count(distinct(WebsiteVisit.besucher_hash)),
                func.count(),
                func.count().filter(WebsiteVisit.art == ART_KONTAKT),
            )
            .where(WebsiteVisit.tag == heute)
            .where(WebsiteVisit.bot.is_(False))
        )).one()

        # Zeitraum ohne heute: aus den Summen.
        zeitraum = (await s.execute(
            select(
                func.coalesce(func.sum(WebsiteTag.besucher), 0),
                func.coalesce(func.sum(WebsiteTag.aufrufe), 0),
                func.coalesce(
                    func.sum(WebsiteTag.aufrufe).filter(
                        WebsiteTag.art == ART_KONTAKT), 0),
            )
            .where(WebsiteTag.tag >= von)
            .where(WebsiteTag.tag < heute)
        )).one()

        bots_heute = (await s.execute(
            select(func.count())
            .where(WebsiteVisit.tag == heute)
            .where(WebsiteVisit.bot.is_(True))
        )).scalar_one()

    besucher_heute, aufrufe_heute, kontakte_heute = heute_zeile
    besucher_zeitraum, aufrufe_zeitraum, kontakte_zeitraum = zeitraum

    # Besucher lassen sich ueber Tage hinweg NICHT eindeutig zusammen-
    # zaehlen (die Kennung wechselt taeglich — genau so ist es gewollt).
    # Die Summe ist deshalb "Besuchstage", nicht "Personen".
    gesamt_besuche = int(besucher_zeitraum) + int(besucher_heute)
    gesamt_kontakte = int(kontakte_zeitraum) + int(kontakte_heute)
    return {
        "tage": tage,
        "besucher_heute": int(besucher_heute),
        "aufrufe_heute": int(aufrufe_heute),
        "kontakte_heute": int(kontakte_heute),
        "besuche_zeitraum": gesamt_besuche,
        "aufrufe_zeitraum": int(aufrufe_zeitraum) + int(aufrufe_heute),
        "kontakte_zeitraum": gesamt_kontakte,
        "kontaktquote": (
            round(gesamt_kontakte * 100 / gesamt_besuche, 1)
            if gesamt_besuche else 0.0
        ),
        "bots_heute": int(bots_heute),
    }


async def verlauf(tage: int = 30) -> list[dict]:
    """Besuche und Kontakt-Klicks je Tag, aeltester Tag zuerst."""
    heute = heute_lokal()
    von = heute - dt.timedelta(days=max(1, tage) - 1)
    async with AsyncSessionLocal() as s:
        aus_summen = (await s.execute(
            select(
                WebsiteTag.tag,
                func.coalesce(func.sum(WebsiteTag.besucher), 0),
                func.coalesce(func.sum(WebsiteTag.aufrufe).filter(
                    WebsiteTag.art == ART_KONTAKT), 0),
            )
            .where(WebsiteTag.tag >= von)
            .where(WebsiteTag.tag < heute)
            .group_by(WebsiteTag.tag)
        )).all()
        heute_zeile = (await s.execute(
            select(
                func.count(distinct(WebsiteVisit.besucher_hash)),
                func.count().filter(WebsiteVisit.art == ART_KONTAKT),
            )
            .where(WebsiteVisit.tag == heute)
            .where(WebsiteVisit.bot.is_(False))
        )).one()

    je_tag = {t: (int(b), int(k)) for t, b, k in aus_summen}
    je_tag[heute] = (int(heute_zeile[0]), int(heute_zeile[1]))
    raus = []
    tag = von
    while tag <= heute:
        besucher, kontakte = je_tag.get(tag, (0, 0))
        raus.append({
            "tag": tag.isoformat(),
            "besucher": besucher,
            "kontakte": kontakte,
        })
        tag += dt.timedelta(days=1)
    return raus


async def herkunft(tage: int = 30, limit: int = 8) -> list[dict]:
    """Woher die Besucher kamen. Leerer Verweis = direkt aufgerufen."""
    von = heute_lokal() - dt.timedelta(days=max(1, tage) - 1)
    async with AsyncSessionLocal() as s:
        zeilen = (await s.execute(
            select(WebsiteTag.ref_host,
                   func.coalesce(func.sum(WebsiteTag.besucher), 0).label("n"))
            .where(WebsiteTag.tag >= von)
            .where(WebsiteTag.art == ART_AUFRUF)
            .group_by(WebsiteTag.ref_host)
            .order_by(func.sum(WebsiteTag.besucher).desc())
            .limit(limit)
        )).all()
    return [
        {"quelle": host or "direkt", "besucher": int(n)} for host, n in zeilen
    ]


async def top_seiten(tage: int = 30, limit: int = 8) -> list[dict]:
    von = heute_lokal() - dt.timedelta(days=max(1, tage) - 1)
    async with AsyncSessionLocal() as s:
        zeilen = (await s.execute(
            select(WebsiteTag.pfad,
                   func.coalesce(func.sum(WebsiteTag.aufrufe), 0).label("n"))
            .where(WebsiteTag.tag >= von)
            .where(WebsiteTag.art == ART_AUFRUF)
            .group_by(WebsiteTag.pfad)
            .order_by(func.sum(WebsiteTag.aufrufe).desc())
            .limit(limit)
        )).all()
    return [{"pfad": pfad, "aufrufe": int(n)} for pfad, n in zeilen]


async def trichter(tage: int = 30) -> dict:
    """Besuch → Kontakt-Klick → Betrieb angelegt.

    Ausdruecklich ein STUFENVERGLEICH, keine Zuordnung: es wird nirgends
    verfolgt, ob ein bestimmter Besucher spaeter Kunde wurde. Die dritte
    Stufe zaehlt schlicht die im Zeitraum angelegten Betriebe.
    """
    from core.models import Tenant

    zahlen = await kennzahlen(tage)
    von = heute_lokal() - dt.timedelta(days=max(1, tage) - 1)
    async with AsyncSessionLocal() as s:
        neue_betriebe = (await s.execute(
            select(func.count()).select_from(Tenant)
            .where(cast(Tenant.created_at, Date) >= von)
        )).scalar_one()
    return {
        "besuche": zahlen["besuche_zeitraum"],
        "kontakte": zahlen["kontakte_zeitraum"],
        "betriebe": int(neue_betriebe),
    }
