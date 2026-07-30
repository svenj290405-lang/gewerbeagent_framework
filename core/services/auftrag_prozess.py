"""Auftragsprozess — Kern-Lifecycle + eigene Zwischenschritte zusammenfuehren.

EINE Quelle der Wahrheit fuer "wie sieht der Ablauf eines Auftrags aus":
die fuenf unveraenderlichen Kern-Schritte aus ``AUFTRAG_LIFECYCLE`` plus
die tenant-eigenen Schritte aus ``auftrag_prozess_schritte``, verwoben zu
einer geordneten Liste. Sowohl die Fortschrittszeile in der Auftrags-
Detailansicht als auch das Aktivitaetsdiagramm im Prozess-Editor lesen
hier — damit koennen die beiden Ansichten nicht auseinanderlaufen.

Erledigt-Semantik:
* **Kern-Schritt** — abgeleitet aus ``angebote.status``. Alles vor dem
  aktuellen Status ist erledigt, der aktuelle ist aktiv, danach offen.
  Nichts wird gespeichert; der Status IST der Zustand.
* **Eigener Schritt** — manuell abgehakt, Zeile in
  ``auftrag_schritt_status``. Bewusst unabhaengig vom Kern-Status: der
  Handwerker kann "Material bestellen" abhaken, lange bevor der Auftrag
  in "Arbeit laeuft" wandert.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, select

from core.database.connection import get_session
from core.models.angebot import (
    ANGEBOT_STATUS_ABGEBROCHEN,
    AUFTRAG_LIFECYCLE,
    AUFTRAG_LIFECYCLE_LABELS,
    Angebot,
)
from core.models.auftrag_prozess import (
    MAX_EIGENE_SCHRITTE,
    MAX_SCHRITT_LABEL,
    AuftragProzessSchritt,
    AuftragSchrittStatus,
)

# Zustaende eines Schritts in der Fortschrittszeile.
SCHRITT_ERLEDIGT = "erledigt"
SCHRITT_AKTIV = "aktiv"
SCHRITT_OFFEN = "offen"


def _kern_schritte() -> list[dict]:
    """Die unveraenderlichen Kern-Schritte als Diagramm-Boxen."""
    return [
        {
            "id": status,
            "typ": "kern",
            "label": AUFTRAG_LIFECYCLE_LABELS.get(status, status),
            "kern_status": status,
            "nach_kern_status": None,
            "gesperrt": True,
        }
        for status in AUFTRAG_LIFECYCLE
    ]


async def lade_prozess(tenant_id: uuid.UUID) -> list[dict]:
    """Die Prozess-Definition des Tenants als geordnete Schritt-Liste.

    Reihenfolge: erst die eigenen Schritte ohne Anker (= ganz vorne),
    dann je Kern-Schritt der Kern selbst, gefolgt von seinen eigenen
    Schritten nach ``sort_index``.
    """
    async with get_session() as s:
        eigene = (await s.execute(
            select(AuftragProzessSchritt)
            .where(AuftragProzessSchritt.tenant_id == tenant_id)
            .order_by(
                AuftragProzessSchritt.sort_index.asc(),
                AuftragProzessSchritt.created_at.asc(),
            )
        )).scalars().all()

    # Eigene Schritte nach Anker gruppieren. Ein Anker, den es im
    # Lifecycle nicht (mehr) gibt, wird auf "ganz vorne" zurueckgeholt,
    # damit kein Schritt unsichtbar verschwindet.
    gueltige_anker = set(AUFTRAG_LIFECYCLE)
    nach_anker: dict[str | None, list[dict]] = {}
    for e in eigene:
        anker = e.nach_kern_status if e.nach_kern_status in gueltige_anker else None
        nach_anker.setdefault(anker, []).append({
            "id": str(e.id),
            "typ": "eigen",
            "label": e.label,
            "kern_status": None,
            "nach_kern_status": anker,
            "gesperrt": False,
        })

    out: list[dict] = list(nach_anker.get(None, []))
    for kern in _kern_schritte():
        out.append(kern)
        out.extend(nach_anker.get(kern["kern_status"], []))
    return out


async def lade_auftrag_schritte(
    tenant_id: uuid.UUID, angebot: Angebot,
) -> list[dict]:
    """Prozess-Schritte EINES Auftrags inkl. Erledigt-Zustand.

    Liefert die Liste aus :func:`lade_prozess`, angereichert um
    ``zustand`` (erledigt/aktiv/offen) und ``erledigt_am``.
    """
    schritte = await lade_prozess(tenant_id)

    async with get_session() as s:
        haken = (await s.execute(
            select(AuftragSchrittStatus)
            .where(AuftragSchrittStatus.tenant_id == tenant_id)
            .where(AuftragSchrittStatus.angebot_id == angebot.id)
        )).scalars().all()
    erledigt_map = {str(h.schritt_id): h.erledigt_am for h in haken}

    abgebrochen = angebot.status == ANGEBOT_STATUS_ABGEBROCHEN
    try:
        aktiv_idx = AUFTRAG_LIFECYCLE.index(angebot.status)
    except ValueError:
        # Status ausserhalb des Lifecycles (z.B. mail_sent, abgebrochen)
        # -> noch kein Kern-Schritt erreicht.
        aktiv_idx = -1

    for sch in schritte:
        if sch["typ"] == "kern":
            kern_idx = AUFTRAG_LIFECYCLE.index(sch["kern_status"])
            if abgebrochen:
                zustand = SCHRITT_OFFEN
            elif kern_idx < aktiv_idx:
                zustand = SCHRITT_ERLEDIGT
            elif kern_idx == aktiv_idx:
                # Der letzte Lifecycle-Schritt (Rechnung raus) ist mit
                # dem Erreichen auch abgeschlossen — es kommt nichts mehr.
                zustand = (
                    SCHRITT_ERLEDIGT
                    if kern_idx == len(AUFTRAG_LIFECYCLE) - 1
                    else SCHRITT_AKTIV
                )
            else:
                zustand = SCHRITT_OFFEN
            sch["zustand"] = zustand
            sch["erledigt_am"] = None
        else:
            am = erledigt_map.get(sch["id"])
            sch["zustand"] = SCHRITT_ERLEDIGT if am else SCHRITT_OFFEN
            sch["erledigt_am"] = am.isoformat() if am else None
    return schritte


class ProzessFehler(ValueError):
    """Ungueltige Prozess-Definition vom Client."""


async def speichere_prozess(
    tenant_id: uuid.UUID, schritte: list[dict],
) -> list[dict]:
    """Ersetzt die eigenen Schritte des Tenants durch ``schritte``.

    Erwartet die VOLLE Liste aus dem Editor (Kern-Schritte duerfen
    mitgeschickt werden, werden aber ignoriert — sie stehen im Code).
    Die Position eines eigenen Schritts ergibt sich aus seiner Stelle in
    der Liste: der zuletzt davor gesehene Kern-Schritt wird sein Anker.
    Dadurch kann ein fehlerhafter Client die Kern-Reihenfolge nicht
    beschaedigen — sie wird gar nicht erst aus dem Request gelesen.

    Schritte mit bestehender ``id`` werden aktualisiert (Haken bleiben
    erhalten), unbekannte/fehlende IDs neu angelegt, nicht mehr
    enthaltene geloescht (inkl. ihrer Haken via ON DELETE CASCADE).
    """
    if not isinstance(schritte, list):
        raise ProzessFehler("Schritt-Liste erwartet.")

    aktueller_anker: str | None = None
    gewuenscht: list[dict] = []
    for roh in schritte:
        if not isinstance(roh, dict):
            raise ProzessFehler("Ungueltiger Schritt.")
        if (roh.get("typ") or "") == "kern" or roh.get("kern_status"):
            kern = roh.get("kern_status") or roh.get("id")
            if kern in AUFTRAG_LIFECYCLE:
                aktueller_anker = kern
            continue
        label = (roh.get("label") or "").strip()
        if not label:
            raise ProzessFehler("Jeder eigene Schritt braucht einen Namen.")
        if len(label) > MAX_SCHRITT_LABEL:
            raise ProzessFehler(
                f"Schritt-Name zu lang (max {MAX_SCHRITT_LABEL} Zeichen).")
        sid = roh.get("id")
        try:
            sid = uuid.UUID(str(sid)) if sid else None
        except (ValueError, TypeError):
            sid = None  # Client-seitige Temp-ID -> Neuanlage
        gewuenscht.append({
            "id": sid, "label": label, "anker": aktueller_anker,
            "sort_index": len(gewuenscht),
        })

    if len(gewuenscht) > MAX_EIGENE_SCHRITTE:
        raise ProzessFehler(
            f"Maximal {MAX_EIGENE_SCHRITTE} eigene Schritte moeglich.")

    async with get_session() as s:
        bestand = {
            r.id: r for r in (await s.execute(
                select(AuftragProzessSchritt)
                .where(AuftragProzessSchritt.tenant_id == tenant_id)
            )).scalars().all()
        }
        behalten: set[uuid.UUID] = set()
        for g in gewuenscht:
            row = bestand.get(g["id"]) if g["id"] else None
            if row is None:
                row = AuftragProzessSchritt(tenant_id=tenant_id)
                s.add(row)
            else:
                behalten.add(row.id)
            row.label = g["label"]
            row.nach_kern_status = g["anker"]
            row.sort_index = g["sort_index"]

        entfernt = [rid for rid in bestand if rid not in behalten]
        if entfernt:
            await s.execute(
                delete(AuftragProzessSchritt)
                .where(AuftragProzessSchritt.tenant_id == tenant_id)
                .where(AuftragProzessSchritt.id.in_(entfernt))
            )
        await s.commit()

    return await lade_prozess(tenant_id)


async def setze_schritt_erledigt(
    tenant_id: uuid.UUID,
    angebot_id: uuid.UUID,
    schritt_id: uuid.UUID,
    *,
    erledigt: bool,
    employee_id: uuid.UUID | None = None,
) -> bool:
    """Hakt einen eigenen Schritt fuer einen Auftrag ab (oder nimmt den
    Haken weg). Returns True bei Erfolg, False wenn Auftrag oder Schritt
    nicht zum Tenant gehoeren."""
    async with get_session() as s:
        gehoert_dazu = (await s.execute(
            select(Angebot.id)
            .where(Angebot.id == angebot_id, Angebot.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if gehoert_dazu is None:
            return False
        schritt = (await s.execute(
            select(AuftragProzessSchritt.id)
            .where(AuftragProzessSchritt.id == schritt_id)
            .where(AuftragProzessSchritt.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if schritt is None:
            return False

        vorhanden = (await s.execute(
            select(AuftragSchrittStatus)
            .where(AuftragSchrittStatus.angebot_id == angebot_id)
            .where(AuftragSchrittStatus.schritt_id == schritt_id)
        )).scalar_one_or_none()

        if erledigt and vorhanden is None:
            s.add(AuftragSchrittStatus(
                tenant_id=tenant_id, angebot_id=angebot_id,
                schritt_id=schritt_id,
                erledigt_am=dt.datetime.now(dt.timezone.utc),
                erledigt_von_employee_id=employee_id,
            ))
        elif not erledigt and vorhanden is not None:
            await s.delete(vorhanden)
        await s.commit()
    return True


__all__ = [
    "ProzessFehler",
    "SCHRITT_AKTIV",
    "SCHRITT_ERLEDIGT",
    "SCHRITT_OFFEN",
    "lade_auftrag_schritte",
    "lade_prozess",
    "setze_schritt_erledigt",
    "speichere_prozess",
]
