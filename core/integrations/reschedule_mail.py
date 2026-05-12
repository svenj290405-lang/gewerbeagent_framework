"""Reschedule-Mail — Kunden-Mail wenn ein Termin wegen Krank/Abwesenheit
auf einen anderen Mitarbeiter umgebucht wird.

Wird von core.integrations.absence_redistribution aufgerufen wenn:
- Ein Mitarbeiter krank gemeldet wurde
- Sein Termin auf einen Kollegen verschoben werden konnte
- Wir den Kunden ueber die Aenderung informieren wollen (Auto-Mail
  laut User-Entscheidung — vollautomatischer Modus)

Reply-Tracking laeuft ueber die existierende Mail-Intake-Pipeline:
message_id + conversation_id werden am Kundengespraech (oder uebergebenem
Track-Objekt) hinterlegt, damit Mail-Replies des Kunden im
EmailConversation-Sticky-Routing dem neuen Mitarbeiter zugeordnet werden.

Throttle: pro Kundengespraech max 1 Reschedule-Mail. Vor Versand
pruefen, ob `reschedule_mail_message_id` schon gesetzt ist (Aufrufer-
Pflicht — dieses Modul macht den Versand idempotent indem es bei
existierender ID einfach skipped).
"""
from __future__ import annotations

import datetime as dt
import logging
from uuid import UUID

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.integrations.microsoft import send_tracked_mail
from core.models.kundengespraech import Kundengespraech

logger = logging.getLogger(__name__)


def _format_dt(dt_val: dt.datetime | None) -> str:
    """13.05.2026 um 14:00 — fuer Mail-Body."""
    if dt_val is None:
        return "(kein Termin)"
    return dt_val.strftime("%d.%m.%Y um %H:%M")


def _build_reschedule_mail_html(
    *,
    company_name: str,
    company_contact_name: str,
    kunde_name: str,
    old_dt: dt.datetime | None,
    new_dt: dt.datetime,
    new_emp_name: str,
    grund: str = "Krankheit",
    location: str | None = None,
) -> str:
    """Baut den HTML-Body. Apple-Polish, kein iframe, keine externen
    Resources — funktioniert in jedem Mail-Client."""
    old_zeile = (
        f"<p>Ihr ursprünglicher Termin am <b>{_format_dt(old_dt)}</b> "
        f"muss leider verschoben werden, weil unser Kollege wegen "
        f"<b>{grund}</b> heute ausgefallen ist.</p>"
        if old_dt else
        f"<p>Wegen einer kurzfristigen Aenderung in unserem Team "
        f"verschiebt sich Ihr Termin.</p>"
    )
    location_zeile = (
        f"<li><b>Ort:</b> {location}</li>" if location else ""
    )
    return f"""<!doctype html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:#222; line-height:1.5; max-width:600px; margin:auto;">
<p>Hallo {kunde_name},</p>

{old_zeile}

<p><b>Neuer Termin:</b></p>
<ul>
  <li><b>Datum:</b> {_format_dt(new_dt)}</li>
  <li><b>Bearbeiter:</b> {new_emp_name}</li>
  {location_zeile}
</ul>

<p>Bitte antworten Sie auf diese Mail mit <i>«passt»</i> wenn der neue Termin
fuer Sie funktioniert — oder schlagen Sie eine andere Zeit vor. Wir
melden uns dann umgehend.</p>

<p>Vielen Dank fuer Ihr Verstaendnis.</p>

<p>Mit freundlichen Gruessen<br>
{company_contact_name}<br>
{company_name}</p>
</body></html>
"""


async def send_reschedule_notice(
    *,
    tenant_id: UUID,
    company_name: str,
    company_contact_name: str,
    kunde_email: str,
    kunde_name: str,
    new_emp_name: str,
    new_emp_id: UUID,
    new_dt: dt.datetime,
    old_dt: dt.datetime | None = None,
    grund: str = "Krankheit",
    location: str | None = None,
    kundengespraech_id: UUID | None = None,
) -> dict:
    """Versendet die Reschedule-Mail und schreibt message_id +
    conversation_id zurueck.

    Wenn `kundengespraech_id` gesetzt: throttle ueber existing
    `reschedule_mail_message_id` (kein Doppel-Versand). Sonst macht
    der Caller das Throttling.

    Returns: {success, message_id, conversation_id, skipped, error}.
    """
    out = {
        "success": False, "message_id": None, "conversation_id": None,
        "skipped": False, "error": None,
    }
    if not kunde_email or "@" not in kunde_email:
        out["error"] = "Keine gueltige Kunden-Mail-Adresse"
        return out

    # Throttle: schon gesendet?
    existing_gespraech: Kundengespraech | None = None
    if kundengespraech_id:
        async with AsyncSessionLocal() as s:
            existing_gespraech = (await s.execute(
                select(Kundengespraech).where(
                    Kundengespraech.id == kundengespraech_id
                )
            )).scalar_one_or_none()
            if existing_gespraech and existing_gespraech.reschedule_mail_message_id:
                out["skipped"] = True
                out["message_id"] = existing_gespraech.reschedule_mail_message_id
                out["conversation_id"] = (
                    existing_gespraech.reschedule_mail_conversation_id
                )
                logger.info(
                    f"reschedule_mail: skip duplicate fuer kg={kundengespraech_id}"
                )
                return out

    body_html = _build_reschedule_mail_html(
        company_name=company_name,
        company_contact_name=company_contact_name,
        kunde_name=kunde_name,
        old_dt=old_dt,
        new_dt=new_dt,
        new_emp_name=new_emp_name,
        grund=grund,
        location=location,
    )
    subject = f"Neuer Termin: {new_dt.strftime('%d.%m.%Y %H:%M')}"

    # Mail via Microsoft Graph (im Namen des neuen Mitarbeiters — der
    # ist ab jetzt zustaendig, Replies sollen bei ihm landen)
    result = await send_tracked_mail(
        tenant_id=tenant_id,
        to_email=kunde_email,
        subject=subject,
        body_html=body_html,
        employee_id=new_emp_id,
    )
    if not result.get("success"):
        out["error"] = result.get("error") or "send_tracked_mail failed"
        logger.warning(
            f"reschedule_mail: Versand fehlgeschlagen tenant={tenant_id} "
            f"kunde={kunde_email}: {out['error']}"
        )
        return out

    out["success"] = True
    out["message_id"] = result.get("message_id")
    out["conversation_id"] = result.get("conversation_id")

    # Auf Kundengespraech persistieren
    if existing_gespraech is not None:
        async with AsyncSessionLocal() as s:
            kg = (await s.execute(
                select(Kundengespraech).where(
                    Kundengespraech.id == kundengespraech_id
                )
            )).scalar_one_or_none()
            if kg is not None:
                kg.reschedule_mail_message_id = out["message_id"]
                kg.reschedule_mail_conversation_id = out["conversation_id"]
                await s.commit()
    logger.info(
        f"reschedule_mail: OK tenant={tenant_id} kunde={kunde_email} "
        f"msg_id={out['message_id']}"
    )
    return out
