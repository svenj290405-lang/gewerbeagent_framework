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


def _ist_stunden_einheit(einheit: str | None) -> bool:
    """Erkennt Stunden-Einheiten aus dem Angebot: „Std", „Stunde(n)", „h"."""
    e = (einheit or "").strip().lower().rstrip(".")
    return e in ("std", "stunde", "stunden", "h", "std") or e.startswith("stunde") or e.startswith("std")


def stunden_abgleich(positionen, gebucht_gesamt) -> dict | None:
    """Vergleicht die im Angebot kalkulierten Stunden mit den real gebuchten.

    Der klassische Handwerker-Geldverlust: das Angebot nennt 8 Stunden, real
    gebucht sind 11, die Rechnung geht mit 8 raus. Q kennt beide Zahlen — hier
    stellt es sie gegenueber. NUR ein Hinweis, kein automatischer Eingriff in
    die Rechnung; die Entscheidung (Mehrstunden abrechnen, ggf. mit
    Kundenzustimmung) bleibt beim Betrieb.

    ``positionen``: iterierbar mit ``.menge`` und ``.einheit`` (AngebotPosition).
    ``gebucht_gesamt``: Summe der gebuchten Stunden (Decimal/float).

    Returns ``None``, wenn das Angebot keine Stunden-Position hat (dann gibt es
    nichts zu vergleichen) — sonst ein Dict mit den beiden Werten, der Differenz
    und einem fertigen Hinweistext (oder ``hinweis=None``, wenn alles passt).
    """
    angeboten = Decimal(0)
    hat_stunden_position = False
    for p in positionen or []:
        if _ist_stunden_einheit(getattr(p, "einheit", None)):
            hat_stunden_position = True
            try:
                angeboten += Decimal(str(getattr(p, "menge", 0) or 0))
            except (InvalidOperation, ValueError):
                continue
    if not hat_stunden_position:
        return None

    try:
        gebucht = Decimal(str(gebucht_gesamt or 0))
    except (InvalidOperation, ValueError):
        gebucht = Decimal(0)

    differenz = gebucht - angeboten
    # Kleine Rundungsreste (< 0,25 h) sind kein Handlungsbedarf.
    if abs(differenz) < Decimal("0.25"):
        hinweis = None
    elif differenz > 0:
        hinweis = (
            f"Es wurden {fmt_stunden(gebucht)} gearbeitet, im Angebot stehen "
            f"aber nur {fmt_stunden(angeboten)}. Prüfe, ob du die "
            f"{fmt_stunden(differenz)} Mehr abrechnest."
        )
    else:
        hinweis = (
            f"Im Angebot stehen {fmt_stunden(angeboten)}, gebucht sind erst "
            f"{fmt_stunden(gebucht)}."
        )

    return {
        "angeboten": fmt_stunden(angeboten),
        "gebucht": fmt_stunden(gebucht),
        "differenz": fmt_stunden(differenz),
        "mehr": differenz > 0,
        "hinweis": hinweis,
    }


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
    logger.info("Stunden gebucht: auftrag=%s tenant=%s %s h von mitarbeiter=%s",
                angebot_id, tenant_id, stunden, employee_id)
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


# Wie viele Buchungen die SUMME umfasst. Vorher stand hier 200 — und die
# Gesamtstunden wurden ueber genau diese Liste gerechnet: bei einem lange
# laufenden Auftrag waren sowohl die Gesamtsumme als auch der Abgleich mit
# den angebotenen Stunden stillschweigend zu niedrig (Audit 2026-08-24).
# Die ANGEZEIGTE Liste bleibt kurz, die Summe stimmt.
_STUNDEN_MAX = 2000
_STUNDEN_ANZEIGE = 200


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
            .limit(_STUNDEN_MAX)
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
        } for e in eintraege[:_STUNDEN_ANZEIGE]],
        "eintraege_gekappt": max(0, len(eintraege) - _STUNDEN_ANZEIGE),
    }


__all__ = [
    "buche_stunden",
    "fmt_stunden",
    "loesche_stunden",
    "parse_stunden",
    "stunden_uebersicht",
    "summen_je_auftrag",
]
