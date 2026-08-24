"""Mengen-Deckel fuer Termine, die eine Kundenmail selbst bucht.

Warum es das gibt: eine eingehende Mail kann bei uns ohne Rueckfrage
einen Kalendereintrag erzeugen. Die Gates davor sind eng — voller Name
und Telefonnummer sind Pflicht, ein zweiter Termin pro Kunde wird
abgelehnt, der Spam-Throttle stoppt Vielschreiber. Was fehlte, war ein
Deckel auf die MENGE: mit zehn Wegwerf-Adressen liessen sich zehn
Termine buchen, jeder fuer sich regelkonform. Der Kalender des Betriebs
ist danach voll, und der Inhaber erfaehrt es erst beim Hinsehen.

Der Deckel wirkt bewusst nur auf die SCHREIBENDEN Aktionen
(BOOK_SLOT / BOOK_DIRECT). Slot-Vorschlaege laufen weiter — sie fassen
den Kalender nicht an, und ein Kunde, der gerade Vorschlaege bekommt,
soll nicht mitten im Dialog verstummen.

Greift der Deckel, wird nicht geschwiegen: Q antwortet dem Kunden
textlich (der Inhaber meldet sich), und der Inhaber bekommt eine
Push-Nachricht. Ein stiller Deckel waere schlimmer als keiner.

Schwellwerte sind Code-Konstanten und bewusst nicht ueber ToolConfig
editierbar — das hier ist Missbrauchsschutz, keine Einstellung.
"""
from __future__ import annotations

import datetime as dt
import logging
from uuid import UUID

from sqlalchemy import func, select

from core.database.connection import get_session
from core.models.email_conversation import EmailConversation

logger = logging.getLogger(__name__)


# Ein Handwerksbetrieb bucht keine 12 Fremd-Termine am Tag per Mail.
# Grosszuegig genug fuer einen echten Ansturm, eng genug dass ein
# Missbrauch auffaellt bevor die Woche zu ist.
MAX_MAIL_BOOKINGS_PER_TENANT_PER_DAY = 12

# Kurzfrist-Deckel gegen den Schwarm: viele Adressen gleichzeitig.
MAX_MAIL_BOOKINGS_PER_TENANT_PER_HOUR = 4


async def count_mail_bookings(
    *,
    tenant_id: UUID,
    window_hours: int,
) -> int:
    """Zaehlt die Termine, die Q im Zeitfenster selbst aus Mails gebucht
    hat. Quelle ist ``email_conversations.booked_at`` — gesetzt genau
    dann, wenn eine Buchung wirklich im Kalender gelandet ist."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
    async with get_session() as s:
        stmt = (
            select(func.count(EmailConversation.id))
            .where(EmailConversation.tenant_id == tenant_id)
            .where(EmailConversation.booked_at.is_not(None))
            .where(EmailConversation.booked_at >= since)
        )
        return int((await s.execute(stmt)).scalar() or 0)


async def should_throttle_booking(
    *, tenant_id: UUID,
) -> tuple[bool, str | None]:
    """True + Grund, wenn diese Mail KEINEN Termin mehr buchen darf.

    Gruende:
    - 'tages-deckel':  >= 12 automatische Mail-Buchungen in 24 h
    - 'stunden-deckel': >= 4 in der letzten Stunde
    """
    letzte_stunde = await count_mail_bookings(
        tenant_id=tenant_id, window_hours=1,
    )
    if letzte_stunde >= MAX_MAIL_BOOKINGS_PER_TENANT_PER_HOUR:
        return True, "stunden-deckel"

    letzter_tag = await count_mail_bookings(
        tenant_id=tenant_id, window_hours=24,
    )
    if letzter_tag >= MAX_MAIL_BOOKINGS_PER_TENANT_PER_DAY:
        return True, "tages-deckel"

    return False, None


async def warn_inhaber_ueber_deckel(
    *, tenant_id: UUID, grund: str, kunde_email: str,
) -> None:
    """Push an den Inhaber, wenn der Deckel gegriffen hat.

    Failsafe: schlaegt der Push fehl, laeuft die Mail-Verarbeitung
    trotzdem weiter — der Deckel selbst haelt ja bereits.
    """
    fenster = "der letzten Stunde" if grund == "stunden-deckel" else "24 Stunden"
    try:
        from core.integrations.push_notifier import send_push_to_tenant

        await send_push_to_tenant(
            tenant_id,
            title="Ungewöhnlich viele Termine per Mail",
            body=(
                f"In {fenster} wurden auffällig viele Termine automatisch "
                f"aus Kundenmails gebucht. Weitere Mail-Buchungen sind "
                f"vorerst gestoppt — bitte kurz in den Kalender schauen."
            ),
            url="/app#anfragen",
            tag="termin-deckel",
            inhaber_only=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"termin-deckel: Push an Inhaber fehlgeschlagen: {e}")

    logger.warning(
        "termin-deckel (%s) greift fuer tenant=%s — Buchung aus Mail von %s "
        "abgelehnt", grund, tenant_id, kunde_email,
    )
