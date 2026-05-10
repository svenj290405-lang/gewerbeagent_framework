"""
Mail-Auto-Reply-Throttle.

Verhindert dass ein einzelner Absender (z.B. ein Spammer mit Mail-
Bombing-Tool) den Tenant in eine Endlos-Antwort-Schleife treibt.

Logik:
- Wir tracken jede ausgehende Auto-Reply als api_usage_log-Zeile
  (Provider 'brevo' / 'microsoft', Unit 'mail_send'). Das passiert
  bereits durch den bestehenden Billing-Code.
- Vor dem naechsten Auto-Reply zaehlen wir die Antworten an
  denselben Empfaenger im letzten Fenster (24h, max 10).
- Ueberschreitet -> Auto-Reply unterbinden, Tenant sieht die Mail
  manuell ueber die Outlook-Kategorie.

Schwellwerte sind ueber API_PRICING_CONFIG-Notes nicht editierbar
(absichtlich Code-Konstanten - das ist Sicherheits-Schutz, kein Preis).
"""
from __future__ import annotations

import datetime as dt
import logging
from uuid import UUID

from sqlalchemy import func, select

from core.database.connection import get_session
from core.models.admin import ApiUsageLog

logger = logging.getLogger(__name__)


# Maximale Auto-Replies an EINEN Empfaenger pro 24h.
# 10 ist grosszuegig fuer normale Korrespondenz, blockt aber einen
# Spammer der z.B. 1000 Mails reinschickt.
MAX_REPLIES_PER_SENDER_PER_DAY = 10

# Globaler Cap: max Auto-Replies pro Tenant pro Stunde.
# Schutz fuer den Fall dass viele verschiedene Spammer parallel feuern.
MAX_REPLIES_PER_TENANT_PER_HOUR = 60


async def count_recent_replies_to(
    *,
    tenant_id: UUID,
    sender_email: str,
    window_hours: int = 24,
) -> int:
    """Zaehlt wie viele Mails wir in den letzten N Stunden an
    `sender_email` rausgeschickt haben (egal ob Brevo oder Microsoft).

    Quelle: api_usage_log mit unit='mail_send' und metadata.recipient
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
    sender_lc = (sender_email or "").lower().strip()
    if not sender_lc:
        return 0

    async with get_session() as s:
        stmt = (
            select(func.count(ApiUsageLog.id))
            .where(ApiUsageLog.tenant_id == tenant_id)
            .where(ApiUsageLog.unit == "mail_send")
            .where(ApiUsageLog.created_at >= since)
            .where(
                ApiUsageLog.metadata_json["recipient"].astext == sender_lc
            )
        )
        return int((await s.execute(stmt)).scalar() or 0)


async def count_tenant_replies(
    *,
    tenant_id: UUID,
    window_hours: int = 1,
) -> int:
    """Globaler Cap: Antworten dieses Tenants ueber alle Empfaenger."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
    async with get_session() as s:
        stmt = (
            select(func.count(ApiUsageLog.id))
            .where(ApiUsageLog.tenant_id == tenant_id)
            .where(ApiUsageLog.unit == "mail_send")
            .where(ApiUsageLog.created_at >= since)
        )
        return int((await s.execute(stmt)).scalar() or 0)


async def should_throttle_reply(
    *, tenant_id: UUID, recipient_email: str,
) -> tuple[bool, str | None]:
    """True + Reason wenn Auto-Reply unterbunden werden soll.

    Reasons:
    - 'per-sender-cap': dieser Empfaenger hat schon >= 10/24h
    - 'per-tenant-cap': Tenant insgesamt hat >= 60/1h
    """
    per_sender = await count_recent_replies_to(
        tenant_id=tenant_id, sender_email=recipient_email,
    )
    if per_sender >= MAX_REPLIES_PER_SENDER_PER_DAY:
        return True, "per-sender-cap"

    per_tenant = await count_tenant_replies(tenant_id=tenant_id)
    if per_tenant >= MAX_REPLIES_PER_TENANT_PER_HOUR:
        return True, "per-tenant-cap"

    return False, None
