"""Stunden auf einem Auftrag — buchen, loeschen, auswerten.

Die eine schmale Stelle fuer Auftragszeit. Alles, was die Oberflaeche
braucht, kommt hier her: die Buchung unter dem Fortschrittsregler, die
Zeile „wer wie lange" am Auftrag und die Sammelabfrage fuer die Listen.

Zur Abgrenzung Auftragszeit vs. gesetzliche Arbeitszeiterfassung siehe
``core/models/auftrag_stunden.py`` — die steht dort im Modul-Docstring,
weil sie am Datenmodell haengt und nicht an dieser Logik.

Sammelabfrage: Listen (Aktuelles, Auftraege) zeigen die Summe an jedem
laufenden Auftrag. Das laeuft als EIN GROUP BY ueber alle sichtbaren
Auftraege, nicht als Abfrage pro Karte — sonst haette der Screen bei
20 Auftraegen 20 zusaetzliche Roundtrips.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from core.database.connection import get_session
from core.models.auftrag_stunden import (
    AuftragStunden,
    MAX_NOTIZ,
    STUNDEN_MAX,
    STUNDEN_MIN,
)

logger = logging.getLogger(__name__)


def parse_stunden(roh) -> Decimal | None:
    """„6,5" / „6.5" / „6" / 6.5 → Decimal. Ungueltiges → None.

    Komma UND Punkt werden akzeptiert: auf einer deutschen Handytastatur
    tippt niemand freiwillig einen Punkt, und eine Zahl mit Komma darf
    nicht als Fehler zurueckkommen.
    """
    if roh is None:
        return None
    text = str(roh).strip().replace(",", ".")
    if not text:
        return None
    try:
        wert = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    wert = wert.quantize(Decimal("0.01"))
    if wert < STUNDEN_MIN or wert > STUNDEN_MAX:
        return None
    return wert


def fmt_stunden(wert) -> str:
    """Decimal/float → „6,5 h"; ganze Stunden ohne Nachkomma („8 h")."""
    try:
        zahl = float(wert or 0)
    except (TypeError, ValueError):
        return "0 h"
    text = f"{zahl:.2f}".rstrip("0").rstrip(".")
    return f"{text.replace('.', ',')} h"


async def buche_stunden(
    tenant_id: uuid.UUID,
    angebot_id: uuid.UUID,
    *,
    employee_id: uuid.UUID | None,
    employee_name: str,
    stunden: Decimal,
    datum: dt.date | None = None,
    notiz: str | None = None,
) -> uuid.UUID:
    """Schreibt eine Stundenbuchung. Der Aufrufer hat den Auftrag bereits
    tenant-geprueft."""
    eintrag = AuftragStunden(
        tenant_id=tenant_id,
        angebot_id=angebot_id,
        employee_id=employee_id,
        employee_name=(employee_name or "Unbekannt")[:200],
        stunden=stunden,
        datum=datum or dt.date.today(),
        notiz=(notiz or "").strip()[:MAX_NOTIZ] or None,
    )
    async with get_session() as s:
        s.add(eintrag)
        await s.flush()
        neue_id = eintrag.id
        await s.commit()
    logger.info("Stunden gebucht: auftrag=%s tenant=%s %s h von %r",
                angebot_id, tenant_id, stunden, employee_name)
    return neue_id


async def loesche_stunden(
    tenant_id: uuid.UUID,
    eintrag_id: uuid.UUID,
    *,
    employee_id: uuid.UUID | None,
    darf_alles: bool,
) -> tuple[bool, str, uuid.UUID | None]:
    """Loescht eine Buchung. Returns ``(ok, fehler, angebot_id)``.

    Jeder raeumt seine eigenen Buchungen auf; der Inhaber alle — er muss
    den Stundennachweis des Betriebs geradeziehen koennen, ohne dass ein
    Mitarbeiter dafuer verfuegbar sein muss.
    """
    async with get_session() as s:
        eintrag = (await s.execute(
            select(AuftragStunden).where(
                AuftragStunden.id == eintrag_id,
                AuftragStunden.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if eintrag is None:
            return False, "Eintrag nicht gefunden.", None
        if not darf_alles and eintrag.employee_id != employee_id:
            return False, "Das sind nicht deine Stunden.", None
        angebot_id = eintrag.angebot_id
        await s.delete(eintrag)
        await s.commit()
    logger.info("Stunden geloescht: eintrag=%s auftrag=%s tenant=%s",
                eintrag_id, angebot_id, tenant_id)
    return True, "", angebot_id


async def summen_je_auftrag(
    tenant_id: uuid.UUID, angebot_ids: list[uuid.UUID],
) -> dict[str, dict]:
    """Sammelabfrage fuer Listen: ``{angebot_id_str: {gesamt, text}}``.

    ``text`` ist die fertige Kurzform fuer die Karte („Sven 6,5 h ·
    Henrik 2 h"), damit die Oberflaeche nicht formatieren muss.
    """
    if not angebot_ids:
        return {}
    async with get_session() as s:
        zeilen = (await s.execute(
            select(
                AuftragStunden.angebot_id,
                AuftragStunden.employee_name,
                func.sum(AuftragStunden.stunden),
            )
            .where(AuftragStunden.tenant_id == tenant_id)
            .where(AuftragStunden.angebot_id.in_(angebot_ids))
            .group_by(AuftragStunden.angebot_id, AuftragStunden.employee_name)
            .order_by(func.sum(AuftragStunden.stunden).desc())
        )).all()

    out: dict[str, dict] = {}
    for angebot_id, name, summe in zeilen:
        eintrag = out.setdefault(
            str(angebot_id), {"gesamt": Decimal(0), "teile": []})
        eintrag["gesamt"] += summe or Decimal(0)
        eintrag["teile"].append(f"{name} {fmt_stunden(summe)}")
    return {
        aid: {
            "gesamt": float(daten["gesamt"]),
            "gesamt_text": fmt_stunden(daten["gesamt"]),
            "text": " · ".join(daten["teile"]),
        }
        for aid, daten in out.items()
    }


async def stunden_uebersicht(
    tenant_id: uuid.UUID, angebot_id: uuid.UUID,
) -> dict:
    """Volle Aufschluesselung fuer die Auftrags-Detailansicht:
    Summe, Summe je Mitarbeiter, Einzelbuchungen (neueste zuerst)."""
    async with get_session() as s:
        eintraege = list((await s.execute(
            select(AuftragStunden)
            .where(AuftragStunden.tenant_id == tenant_id)
            .where(AuftragStunden.angebot_id == angebot_id)
            .order_by(AuftragStunden.datum.desc(),
                      AuftragStunden.created_at.desc())
            .limit(200)
        )).scalars().all())

    je_mitarbeiter: dict[str, dict] = {}
    gesamt = Decimal(0)
    for e in eintraege:
        gesamt += e.stunden or Decimal(0)
        schluessel = str(e.employee_id) if e.employee_id else f"name:{e.employee_name}"
        zeile = je_mitarbeiter.setdefault(
            schluessel, {"name": e.employee_name, "stunden": Decimal(0)})
        zeile["stunden"] += e.stunden or Decimal(0)

    sortiert = sorted(je_mitarbeiter.values(),
                      key=lambda z: z["stunden"], reverse=True)
    return {
        "gesamt": float(gesamt),
        "gesamt_text": fmt_stunden(gesamt),
        "je_mitarbeiter": [{
            "name": z["name"],
            "stunden": float(z["stunden"]),
            "text": fmt_stunden(z["stunden"]),
        } for z in sortiert],
        "eintraege": [{
            "id": str(e.id),
            "name": e.employee_name,
            "employee_id": str(e.employee_id) if e.employee_id else "",
            "stunden": float(e.stunden),
            "text": fmt_stunden(e.stunden),
            "datum": e.datum.strftime("%d.%m.%Y") if e.datum else "",
            "notiz": e.notiz or "",
        } for e in eintraege],
    }


__all__ = [
    "buche_stunden",
    "fmt_stunden",
    "loesche_stunden",
    "parse_stunden",
    "stunden_uebersicht",
    "summen_je_auftrag",
]
