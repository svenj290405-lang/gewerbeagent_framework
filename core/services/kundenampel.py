"""Kundenampel — laeuft bei jedem Betrieb noch alles?

Die Frage, die sich der Betreiber jeden Morgen stellt, war bisher nur mit
Herumklicken zu beantworten: Telefon, Mail und Kalender haengen an
Tokens, die ablaufen; ein Betrieb, der die App zwei Wochen nicht
oeffnet, ist auf dem Absprung; offene Anfragen ohne Antwort sind
unzufriedene Endkunden.

Hier wird nichts neu berechnet — die Bausteine gibt es alle:
`daily_health_check._check_anbindungen()` fuer die Verbindungen,
`AppUsageEvent` fuer die Nutzung, die bestehenden Tabellen fuer offene
Vorgaenge. Neu ist nur, dass daraus eine Zeile mit einer Farbe und einem
Satz Begruendung wird.

Bewusst NICHT verwendet: `Tenant.status`. Der Pilotbetrieb steht seit Mai
auf "onboarding", obwohl er live ist — ein Feld, das niemand pflegt, darf
keine Ampel steuern.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select

from core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

ROT = "rot"
GELB = "gelb"
GRUEN = "gruen"

#: Ab so vielen Tagen ohne Nutzung wird es gelb.
STILL_TAGE = 7
#: Ab so vielen Tagen ohne Token-Nutzung droht der Ablauf (Microsoft: 90).
TOKEN_WARNUNG_TAGE = 60


async def _nutzung_seit(tenant_id, seit: dt.datetime) -> int:
    from core.models import AppUsageEvent
    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(func.count())
            .select_from(AppUsageEvent)
            .where(AppUsageEvent.tenant_id == tenant_id)
            .where(AppUsageEvent.created_at >= seit)
        )).scalar_one()


async def _offene_vorgaenge(tenant_id) -> dict:
    """Was beim Betrieb liegen geblieben ist."""
    from core.models import AnfrageToken, Rueckruf
    from core.models.failed_mail_queue import FailedMailQueue

    async with AsyncSessionLocal() as s:
        anfragen = (await s.execute(
            select(func.count()).select_from(AnfrageToken)
            .where(AnfrageToken.tenant_id == tenant_id)
            .where(AnfrageToken.submitted_at.is_(None))
        )).scalar_one()
        rueckrufe = (await s.execute(
            select(func.count()).select_from(Rueckruf)
            .where(Rueckruf.tenant_id == tenant_id)
            .where(Rueckruf.erledigt_at.is_(None))
        )).scalar_one()
        mails = (await s.execute(
            select(func.count()).select_from(FailedMailQueue)
            .where(FailedMailQueue.tenant_id == tenant_id)
            .where(FailedMailQueue.status != "sent")
        )).scalar_one()
    return {
        "offene_anfragen": int(anfragen),
        "offene_rueckrufe": int(rueckrufe),
        "haengende_mails": int(mails),
    }


def _bewerte(anbindungen: dict, nutzung_7t: int, offen: dict) -> tuple[str, str]:
    """Farbe + ein Satz, warum. Der Satz ist der eigentliche Wert."""
    tote = [
        f"{dienst}"
        for dienst, info in (anbindungen or {}).items()
        if isinstance(info, dict) and not info.get("ok")
        and not info.get("tage_ohne_nutzung")
    ]
    muede = [
        f"{dienst} seit {info['tage_ohne_nutzung']} Tagen ungenutzt"
        for dienst, info in (anbindungen or {}).items()
        if isinstance(info, dict) and not info.get("ok")
        and info.get("tage_ohne_nutzung")
    ]

    if tote:
        return ROT, f"Verbindung antwortet nicht: {', '.join(tote)}."
    if muede:
        return GELB, f"Zugang laeuft ab: {'; '.join(muede)}."
    if not anbindungen:
        return GELB, "Noch keine Anbindung verbunden — Einrichtung unvollstaendig."
    if nutzung_7t == 0:
        return GELB, f"Seit {STILL_TAGE} Tagen keine Nutzung der App."
    if offen["haengende_mails"]:
        return GELB, f"{offen['haengende_mails']} Mail(s) haengen im Versand."
    if offen["offene_rueckrufe"]:
        return GRUEN, f"Laeuft; {offen['offene_rueckrufe']} Rueckruf(e) offen."
    return GRUEN, f"Laeuft; {nutzung_7t} Aktionen in 7 Tagen."


async def ampel_fuer_tenant(tenant) -> dict:
    """Eine Zeile fuer die Kundenansicht."""
    from core.integrations.daily_health_check import _check_anbindungen

    seit = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=STILL_TAGE)
    try:
        _, bericht = await _check_anbindungen(nur_tenant_id=tenant.id)
        anbindungen = bericht.get(tenant.slug, {})
    except Exception as exc:  # noqa: BLE001
        # Eine kaputte Pruefung darf die Uebersicht nicht leeren —
        # dann eben ohne Verbindungsteil, aber sichtbar.
        logger.warning("Ampel: Anbindungspruefung fehlgeschlagen: %s", exc)
        anbindungen = {}

    nutzung = await _nutzung_seit(tenant.id, seit)
    offen = await _offene_vorgaenge(tenant.id)
    farbe, begruendung = _bewerte(anbindungen, nutzung, offen)

    return {
        "id": str(tenant.id),
        "slug": tenant.slug,
        "name": tenant.company_name or tenant.slug,
        "farbe": farbe,
        "begruendung": begruendung,
        "anbindungen": anbindungen,
        "nutzung_7t": nutzung,
        **offen,
    }


async def alle_ampeln() -> list[dict]:
    """Alle Betriebe, Rot zuerst — was brennt, steht oben.

    Das Plattform-Konto `_global` ist kein Kunde und bleibt draussen.
    """
    from core.models import Tenant

    async with AsyncSessionLocal() as s:
        tenants = (await s.execute(
            select(Tenant).where(Tenant.slug != "_global").order_by(Tenant.slug)
        )).scalars().all()
        for t in tenants:
            s.expunge(t)

    zeilen = [await ampel_fuer_tenant(t) for t in tenants]
    rang = {ROT: 0, GELB: 1, GRUEN: 2}
    zeilen.sort(key=lambda z: (rang.get(z["farbe"], 9), z["name"].lower()))
    return zeilen
