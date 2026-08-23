"""Besucherzaehlung der Marketing-Website — eigene, cookielose Messung.

Warum eigenbau statt eines fertigen Dienstes: die Landingpage wirbt mit
"kein Tracking durch Dritte" und die Datenschutzerklaerung sagt "keine
Tracking- oder Analyse-Cookies" zu. Beides bleibt nur wahr, wenn die
Zaehlung auf dem eigenen Server laeuft, ohne Cookie und ohne fremde
Skripte.

Drei Tabellen, jede mit einer Aufgabe:

``WebsiteVisit``  Rohereignisse, **14 Tage**. Genauso lange, wie die
                  Datenschutzerklaerung es fuer Server-Logs zusagt.
``WebsiteTag``    Tagesaggregat, bleibt dauerhaft. Enthaelt keinen Hash
                  und keinen Personenbezug mehr — damit bleiben die
                  Zahlen erhalten, ohne dass Daten aufbewahrt werden.
``WebsiteSalt``   Der taegliche Zufallswert, **2 Tage**.

Der Besucher wird ueber ``sha256(salt + IP + User-Agent)`` erkannt. Weder
IP noch User-Agent werden gespeichert. Der Salt ist bewusst ZUFAELLIG und
nicht aus dem SECRET_KEY abgeleitet: ein abgeleiteter Wert waere fuer
immer nachrechenbar, damit bliebe jeder Hash dauerhaft gegen eine
IP-Liste pruefbar. Nach zwei Tagen ist der Salt geloescht — danach laesst
sich auch mit vollem Datenbankzugriff nicht mehr feststellen, ob eine
bestimmte Person an einem Tag da war.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
import uuid

from sqlalchemy import Date, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base

logger = logging.getLogger(__name__)

# Ereignisarten. Bewusst kurz und geschlossen — was nicht hier steht,
# wird verworfen.
ART_AUFRUF = "aufruf"
ART_KONTAKT = "kontakt"
ALLE_ARTEN = {ART_AUFRUF, ART_KONTAKT}

#: So lange bleiben Rohereignisse liegen (danach nur noch Tagessummen).
ROHDATEN_TAGE = 14
#: So lange bleibt ein Tages-Salt (danach ist kein Hash mehr nachrechenbar).
SALT_TAGE = 2


class WebsiteVisit(Base):
    """Ein einzelnes Ereignis auf der Website."""

    __tablename__ = "website_visits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    # Kalendertag in Europe/Berlin — NICHT UTC. "Besucher heute" muss
    # dem entsprechen, was der Betreiber unter heute versteht.
    tag: Mapped[dt.date] = mapped_column(Date, nullable=False)
    besucher_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    pfad: Mapped[str] = mapped_column(String(120), nullable=False)
    # Nur der Host des Verweises, nie die volle Adresse: fremde
    # Query-Strings koennen personenbezogene Daten enthalten.
    ref_host: Mapped[str | None] = mapped_column(String(120), nullable=True)
    art: Mapped[str] = mapped_column(String(20), nullable=False)
    # Erkannte Automaten werden markiert statt verworfen, damit sichtbar
    # bleibt, wie viel weggefiltert wird.
    bot: Mapped[bool] = mapped_column(default=False, nullable=False)

    __table_args__ = (
        Index("ix_website_visits_tag_art", "tag", "art"),
        Index("ix_website_visits_tag_hash", "tag", "besucher_hash"),
    )


class WebsiteTag(Base):
    """Tagessumme — anonym, bleibt dauerhaft."""

    __tablename__ = "website_tage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tag: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    pfad: Mapped[str] = mapped_column(String(120), nullable=False)
    ref_host: Mapped[str] = mapped_column(
        String(120), nullable=False, server_default="",
    )
    art: Mapped[str] = mapped_column(String(20), nullable=False)
    aufrufe: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    besucher: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "tag", "pfad", "ref_host", "art", name="uq_website_tage",
        ),
    )


class WebsiteSalt(Base):
    """Der Zufallswert eines Tages. Wird nach SALT_TAGE geloescht."""

    __tablename__ = "website_salt"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tag: Mapped[dt.date] = mapped_column(
        Date, nullable=False, unique=True, index=True,
    )
    salt: Mapped[str] = mapped_column(String(64), nullable=False)


# ---------------------------------------------------------------------
# Salt + Hash
# ---------------------------------------------------------------------

# Ein Prozess, ein uvicorn-Worker — ein Eintrag reicht. Die DB-Zeile
# sorgt dafuer, dass ein Neustart mitten am Tag nicht doppelt zaehlt.
_SALT_CACHE: dict[dt.date, str] = {}


def heute_lokal() -> dt.date:
    """Kalendertag in Europe/Berlin."""
    import zoneinfo
    return dt.datetime.now(zoneinfo.ZoneInfo("Europe/Berlin")).date()


async def hole_salt(tag: dt.date) -> str:
    """Salt des Tages, notfalls neu erzeugt.

    Upsert nach demselben Muster wie die Cron-Heartbeats: zwei
    gleichzeitige erste Besucher duerfen nicht zwei Salts erzeugen,
    sonst zaehlt derselbe Mensch doppelt.
    """
    gecached = _SALT_CACHE.get(tag)
    if gecached:
        return gecached

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert
    from core.database import AsyncSessionLocal

    neuer = secrets.token_hex(32)
    async with AsyncSessionLocal() as s:
        await s.execute(
            insert(WebsiteSalt.__table__)
            .values(tag=tag, salt=neuer)
            .on_conflict_do_nothing(index_elements=["tag"])
        )
        await s.commit()
        vorhanden = (await s.execute(
            select(WebsiteSalt.salt).where(WebsiteSalt.tag == tag)
        )).scalar_one()

    _SALT_CACHE.clear()          # nur der heutige Tag wird gebraucht
    _SALT_CACHE[tag] = vorhanden
    return vorhanden


def besucher_hash(salt: str, ip: str, user_agent: str) -> str:
    """Tageskennung eines Besuchers. Weder IP noch User-Agent bleiben uebrig."""
    roh = f"{salt}|{ip}|{user_agent}".encode("utf-8", "replace")
    return hashlib.sha256(roh).hexdigest()[:32]


async def record_website_visit(
    *, tag: dt.date, hash_: str, pfad: str, ref_host: str | None,
    art: str, bot: bool = False,
) -> None:
    """Schreibt EIN Ereignis. **Failsafe** — eine Zaehlung darf nie einen
    Seitenaufruf stoeren, deshalb wird jeder Fehler geschluckt."""
    if art not in ALLE_ARTEN:
        return
    try:
        from core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as s:
            s.add(WebsiteVisit(
                tag=tag, besucher_hash=hash_, pfad=pfad[:120],
                ref_host=(ref_host or None), art=art, bot=bot,
            ))
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_website_visit fehlgeschlagen: %s", exc)
