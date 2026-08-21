"""Angebots-Antworten erkennen: verbindet die Kundenantwort im Postfach mit
einem versandten Angebot und handelt gemaess Automatisierungsgrad.

Der Klassifikator (``classify_angebot_response`` in ``core/ai/gemini.py``)
existiert schon laenger, wurde aber nirgends aufgerufen — der Uebergang
Angebot -> Auftrag war komplett manuell. Dieses Modul schliesst die Luecke.

Zuordnung ist deterministisch, nicht per Fuzzy-Match: ein versandtes Angebot
speichert die Microsoft-Graph ``conversationId`` (``mail_conversation_id``),
und eine eingehende Antwort traegt dieselbe conversationId. Damit ist die
Antwort eindeutig genau EINEM Angebot zugeordnet.

Automatisierungsgrad (Registry-Key ``angebot_antwort``):
    manuell      Q tut nichts (die Mail laeuft wie bisher durch die Pipeline).
    assistiert   Q meldet den Vorschlag per Push — den Status setzt der Chef
                 selbst im Auftrag (Default).
    automatisch  Q setzt den Status direkt (accepted/rejected) und meldet es.

Fail-safe: jeder Fehler wird geschluckt und geloggt. Dieser Pfad haengt an der
Postfach-Verarbeitung und darf sie unter keinen Umstaenden abbrechen.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from core.database.connection import get_session
from core.features.automation_check import mode_for_automation
from core.features.automations import (
    MODE_MANUELL, MODE_ASSISTIERT, MODE_AUTOMATISCH,
)

logger = logging.getLogger(__name__)

# Nur diese Klassifikationen loesen ueberhaupt etwas aus. RUECKFRAGE/UNSICHER
# laufen bewusst NICHT durch — eine Rueckfrage ist keine Zusage, und bei
# Unsicherheit soll der Mensch entscheiden. Die Mail taucht ohnehin im
# normalen Anfrage-/Konversations-Fluss auf.
_ACTIONABLE = {"ANNAHME", "ABLEHNUNG"}
# "low" ist zu wackelig, um darauf einen Status zu bewegen oder den Chef zu
# behelligen — dann lieber gar nichts.
_MIN_CONFIDENCE = {"high", "medium"}


async def detect_and_handle_angebot_reply(
    *,
    tenant_id: uuid.UUID,
    conversation_id: str | None,
    mail_subject: str,
    mail_body: str,
) -> dict | None:
    """Prueft eine eingehende Mail auf eine Angebots-Antwort und handelt.

    Returns ein Ergebnis-Dict wenn gehandelt wurde, sonst ``None`` (kein
    passendes Angebot, Stufe manuell, unklare Klassifikation, Fehler).
    """
    if not conversation_id:
        return None
    try:
        mode = await mode_for_automation(tenant_id, "angebot_antwort")
        if mode == MODE_MANUELL:
            return None

        from core.models.angebot import (
            Angebot, ANGEBOT_STATUS_MAIL_SENT,
        )
        # Deterministische Zuordnung: offenes (mail_sent) Angebot mit genau
        # dieser conversationId. Ordered by mail_sent_at desc, falls in einem
        # Thread mehrere Angebote versandt wurden — das juengste gilt.
        async with get_session() as s:
            angebot = (await s.execute(
                select(Angebot).where(
                    Angebot.tenant_id == tenant_id,
                    Angebot.mail_conversation_id == conversation_id,
                    Angebot.status == ANGEBOT_STATUS_MAIL_SENT,
                ).order_by(Angebot.mail_sent_at.desc())
            )).scalars().first()
        if angebot is None:
            return None

        from core.ai.gemini import classify_angebot_response
        result = await classify_angebot_response(
            mail_subject=mail_subject or "", mail_body=mail_body or "",
        )
        cls = (result or {}).get("classification") or "UNSICHER"
        conf = (result or {}).get("confidence") or "low"
        if cls not in _ACTIONABLE or conf not in _MIN_CONFIDENCE:
            logger.info(
                "angebot_antwort: keine Aktion (angebot=%s cls=%s conf=%s)",
                angebot.id, cls, conf,
            )
            return None

        return await _act(
            tenant_id=tenant_id,
            angebot_id=angebot.id,
            kunde_name=angebot.kunde_name,
            classification=cls,
            mode=mode,
            reason=(result or {}).get("reason") or "",
        )
    except Exception as exc:  # noqa: BLE001
        # Niemals die Postfach-Verarbeitung abbrechen.
        logger.warning("angebot_antwort fehlgeschlagen (tenant=%s): %s",
                       tenant_id, exc)
        return None


async def _act(
    *,
    tenant_id: uuid.UUID,
    angebot_id: uuid.UUID,
    kunde_name: str,
    classification: str,
    mode: str,
    reason: str,
) -> dict:
    import datetime as dt
    from core.models.angebot import (
        Angebot, ANGEBOT_STATUS_ACCEPTED, ANGEBOT_STATUS_REJECTED,
    )

    ist_zusage = classification == "ANNAHME"
    ziel_status = ANGEBOT_STATUS_ACCEPTED if ist_zusage else ANGEBOT_STATUS_REJECTED

    gesetzt = False
    if mode == MODE_AUTOMATISCH:
        # Status direkt setzen — spiegelt exakt den manuellen Schalter in
        # app_screens (a.status = ...; accepted_at bei Zusage).
        async with get_session() as s:
            a = (await s.execute(
                select(Angebot).where(
                    Angebot.id == angebot_id, Angebot.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if a is not None and a.status == "mail_sent":
                a.status = ziel_status
                if ist_zusage and not a.accepted_at:
                    a.accepted_at = dt.datetime.now(dt.timezone.utc)
                elif not ist_zusage and not a.rejected_at:
                    a.rejected_at = dt.datetime.now(dt.timezone.utc)
                await s.commit()
                gesetzt = True
        logger.info(
            "angebot_antwort AUTOMATISCH: angebot=%s -> %s (kunde=%s)",
            angebot_id, ziel_status, kunde_name,
        )

    await _notify(
        tenant_id=tenant_id,
        kunde_name=kunde_name,
        ist_zusage=ist_zusage,
        mode=mode,
        status_gesetzt=gesetzt,
    )
    return {
        "angebot_id": str(angebot_id),
        "classification": classification,
        "mode": mode,
        "status_gesetzt": gesetzt,
        "reason": reason,
    }


async def _notify(
    *,
    tenant_id: uuid.UUID,
    kunde_name: str,
    ist_zusage: bool,
    mode: str,
    status_gesetzt: bool,
) -> None:
    """Push an den Betrieb."""
    try:
        from core.integrations.push_notifier import send_push_to_tenant
        wer = kunde_name or "Ein Kunde"
        if ist_zusage:
            if status_gesetzt:
                title = "✅ Auftrag angenommen"
                body = f"{wer} hat zugesagt — Auftrag wurde gestartet."
            else:
                title = "✅ Kunde hat wohl zugesagt"
                body = f"{wer} hat das Angebot vermutlich angenommen. Auftrag starten?"
        else:
            if status_gesetzt:
                title = "❌ Angebot abgelehnt"
                body = f"{wer} hat abgesagt — als abgelehnt markiert."
            else:
                title = "❌ Kunde hat wohl abgesagt"
                body = f"{wer} hat das Angebot vermutlich abgelehnt."
        await send_push_to_tenant(
            tenant_id, title=title, body=body,
            url="/app#auftraege_page", tag="angebot_antwort",
            inhaber_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("angebot_antwort Push fehlgeschlagen: %s", exc)
