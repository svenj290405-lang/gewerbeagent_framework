"""
Mail-Pipeline Utilities fuer die Microsoft-Pipeline.

Kapselt EmailConversation-Lookups, State-Uebergaenge und Tenant-
Telegram-Pushes — die Bausteine fuer Reply-Threading + Follow-up-
Routing in core/integrations/microsoft_inbox.py.

Bewusst kein Code-Reuse mit plugins/mail_intake/handler.py (Brevo):
die Brevo-Pipeline hat eigene Kopien und wird im Endspiel des Refactors
komplett entfernt (Task A.4). Bis dahin laufen beide Pipelines parallel.

Threading-Strategie (find_open_conversation):
1. Primaer: Microsoft Graph conversationId — gruppiert Threads
   provider-nativ, ueberlebt fehlende In-Reply-To-Header.
2. Sekundaer: In-Reply-To-Header (RFC) match gegen last_message_id —
   1:1-Match auf die zuletzt versendete Q-Reply.
3. Tertiaer: kunde_email + state != CLOSED, juengste Konversation —
   Fallback wenn der Kunde eine komplett neue Mail mit eigenem
   Subject schickt, aber wir noch einen offenen Vorgang haben.

Geschlossene Konversationen (state == STATE_CLOSED) werden in ALLEN
Lookup-Pfaden ignoriert: ein spaeter Reply nach Vorgangs-Ende soll als
neue Anfrage behandelt werden (frisches Anfrage-Formular).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import (
    EmailConversation,
    STATE_AWAITING_CONFIRMATION,
    STATE_CLOSED,
    Tenant,
)

logger = logging.getLogger(__name__)


# ====================================================================
# Header-Parsing
# ====================================================================

def extract_in_reply_to_from_headers(
    headers: Iterable[dict[str, Any]] | None,
) -> str | None:
    """Liest 'In-Reply-To' aus Microsoft Graph internetMessageHeaders.

    internetMessageHeaders ist Array von {name: str, value: str}.
    Header-Namen sind case-insensitive (RFC 5322). Wir matchen
    case-insensitive und nehmen die ERSTE Vorkommnis.

    Returns: Header-Value (typisch "<random@domain>") oder None.
    """
    if not headers:
        return None
    for h in headers:
        name = (h.get("name") or "").strip().lower()
        if name == "in-reply-to":
            val = (h.get("value") or "").strip()
            return val or None
    return None


# ====================================================================
# Conversation-Lookup
# ====================================================================

async def find_open_conversation(
    tenant_id: uuid.UUID,
    sender_email: str,
    *,
    microsoft_conversation_id: str | None = None,
    in_reply_to: str | None = None,
) -> EmailConversation | None:
    """Sucht eine bestehende OFFENE Konversation (state != CLOSED).

    Match-Reihenfolge: ms_conv_id > in_reply_to > sender_email-Fallback.
    Siehe Modul-Docstring fuer Rationale.

    Returns: EmailConversation (expunged aus der Session) oder None.
    """
    sender_email_norm = (sender_email or "").strip().lower()

    async with AsyncSessionLocal() as s:
        # 1. Microsoft conversationId match
        if microsoft_conversation_id:
            r = await s.execute(
                select(EmailConversation)
                .where(
                    EmailConversation.tenant_id == tenant_id,
                    EmailConversation.microsoft_conversation_id
                    == microsoft_conversation_id,
                    EmailConversation.state != STATE_CLOSED,
                )
                .order_by(EmailConversation.updated_at.desc())
                .limit(1)
            )
            conv = r.scalar_one_or_none()
            if conv:
                s.expunge(conv)
                return conv

        # 2. In-Reply-To match auf zuletzt versendete Q-Mail
        if in_reply_to:
            r = await s.execute(
                select(EmailConversation)
                .where(
                    EmailConversation.tenant_id == tenant_id,
                    EmailConversation.last_message_id == in_reply_to,
                    EmailConversation.state != STATE_CLOSED,
                )
                .order_by(EmailConversation.updated_at.desc())
                .limit(1)
            )
            conv = r.scalar_one_or_none()
            if conv:
                s.expunge(conv)
                return conv

        # 3. Fallback per kunde_email
        if sender_email_norm:
            r = await s.execute(
                select(EmailConversation)
                .where(
                    EmailConversation.tenant_id == tenant_id,
                    EmailConversation.kunde_email == sender_email_norm,
                    EmailConversation.state != STATE_CLOSED,
                )
                .order_by(EmailConversation.updated_at.desc())
                .limit(1)
            )
            conv = r.scalar_one_or_none()
            if conv:
                s.expunge(conv)
                return conv

    return None


# ====================================================================
# Conversation-Persistenz
# ====================================================================

async def create_conversation(
    tenant_id: uuid.UUID,
    sender_email: str,
    sender_name: str | None,
    subject: str | None,
    *,
    microsoft_conversation_id: str | None = None,
    assigned_employee_id: uuid.UUID | None = None,
) -> EmailConversation:
    """Legt eine neue Konversation an (state=AWAITING_CONFIRMATION).

    Wird vom Microsoft-Inbox-Handler aufgerufen wenn KEIN bestehender
    Thread gefunden wurde — also typischer Neukunde-Fall.

    Returns: persistierte EmailConversation (expunged).
    """
    async with AsyncSessionLocal() as s:
        conv = EmailConversation(
            tenant_id=tenant_id,
            kunde_email=(sender_email or "").strip().lower(),
            kunde_name=(sender_name or None),
            last_subject=(subject or None) and subject[:500],
            microsoft_conversation_id=microsoft_conversation_id,
            state=STATE_AWAITING_CONFIRMATION,
            assigned_employee_id=assigned_employee_id,
        )
        s.add(conv)
        await s.commit()
        await s.refresh(conv)
        s.expunge(conv)
    logger.info(
        f"mail_pipeline: neue Konversation angelegt id={conv.id} "
        f"tenant={tenant_id} kunde={conv.kunde_email} "
        f"ms_conv_id={(microsoft_conversation_id or '')[:30]}"
    )
    return conv


async def record_inbound(
    conv_id: uuid.UUID,
    *,
    last_user_message: str | None = None,
    classification: str | None = None,
    classification_confidence: str | None = None,
    classification_reason: str | None = None,
    microsoft_conversation_id: str | None = None,
) -> None:
    """Vermerkt eine eingehende Mail an einer bestehenden Konversation.

    last_user_message wird auf 4000 Zeichen begrenzt — der Body kann
    sehr lang sein und das ist Konversations-Memory, nicht der
    DSGVO-Audit-Log.
    """
    import datetime as _dt

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(EmailConversation).where(EmailConversation.id == conv_id)
        )
        conv = r.scalar_one_or_none()
        if not conv:
            logger.warning(
                f"record_inbound: Konversation {conv_id} nicht gefunden"
            )
            return
        if last_user_message is not None:
            conv.last_user_message = last_user_message[:4000]
        if classification is not None:
            conv.classification = classification[:30]
            conv.classified_at = _dt.datetime.now(_dt.timezone.utc)
        if classification_confidence is not None:
            conv.classification_confidence = classification_confidence[:10]
        if classification_reason is not None:
            conv.classification_reason = classification_reason[:1000]
        # ms_conv_id nachtragen falls bei Erstanlage gefehlt
        if (
            microsoft_conversation_id
            and not conv.microsoft_conversation_id
        ):
            conv.microsoft_conversation_id = microsoft_conversation_id
        await s.commit()


async def record_outbound_q_reply(
    conv_id: uuid.UUID,
    *,
    internet_message_id: str | None,
    microsoft_conversation_id: str | None = None,
    q_reply_text: str | None = None,
    subject: str | None = None,
) -> None:
    """Vermerkt eine ausgehende Q-Antwort. Setzt last_message_id auf die
    Microsoft-`internetMessageId` damit der naechste eingehende Reply
    via In-Reply-To gematcht werden kann.

    Wird nach erfolgreichem send_tracked_mail aufgerufen — der liefert
    internet_message_id + conversation_id zurueck.
    """
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(EmailConversation).where(EmailConversation.id == conv_id)
        )
        conv = r.scalar_one_or_none()
        if not conv:
            logger.warning(
                f"record_outbound_q_reply: Konversation {conv_id} nicht gefunden"
            )
            return
        if internet_message_id:
            conv.last_message_id = internet_message_id[:500]
        if microsoft_conversation_id and not conv.microsoft_conversation_id:
            conv.microsoft_conversation_id = microsoft_conversation_id[:255]
        if q_reply_text is not None:
            conv.last_q_reply = q_reply_text[:4000]
        if subject is not None:
            conv.last_subject = subject[:500]
        await s.commit()


async def mark_delivery_failed(
    conv_id: uuid.UUID, *, reason: str | None = None,
) -> None:
    """State auf STATE_DELIVERY_FAILED setzen (Teil G Bounce).

    Wird hier definiert damit Teil G nur Plumbing braucht.
    """
    from core.models import STATE_DELIVERY_FAILED

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(EmailConversation).where(EmailConversation.id == conv_id)
        )
        conv = r.scalar_one_or_none()
        if not conv:
            return
        conv.state = STATE_DELIVERY_FAILED
        if reason:
            # Reason ans Ende des classification_reason haengen damit
            # nichts verloren geht (Audit-Spur in einem Feld).
            old = conv.classification_reason or ""
            sep = " | " if old else ""
            conv.classification_reason = (old + sep + f"BOUNCE: {reason}")[:1000]
        await s.commit()


# ====================================================================
# Tenant-Notification (Telegram-Push fuer Follow-ups)
# ====================================================================

async def push_tenant_followup_mail(
    tenant: Tenant,
    *,
    sender_email: str,
    sender_name: str,
    subject: str,
    body_preview: str,
    conv: EmailConversation,
    employee_id: uuid.UUID | None = None,
) -> bool:
    """Telegram-Push an den Tenant/Mitarbeiter bei Folge-Mail auf
    bestehenden Vorgang.

    Bewusst KEIN Auto-Reply: der Inhaber soll selber entscheiden ob
    er manuell antwortet, Termin storniert (Teil D), etc. Q-Auto-Reply
    waere bei Folge-Mails zu riskant (z.B. "Termin doch nicht moeglich"
    auf eine Anfrage-Bestaetigung wuerde sonst peinlich eine
    Standard-Formular-Mail triggern).

    Schickt an den employee_id wenn gesetzt, sonst an den Konversations-
    Assigned-Employee, sonst an den Tenant-Default.

    Returns: True wenn Push abgeschickt (nicht garantiert dass Telegram
    es ausgeliefert hat), False bei Fehler.
    """
    from html import escape as _h

    target_employee_id = employee_id or conv.assigned_employee_id

    # State-Label damit der Inhaber sieht in welcher Phase die Konv. war
    state_label = {
        STATE_AWAITING_CONFIRMATION: "Anfrage offen",
        "booked": "Termin gebucht",
        "proposing_slots": "Slots vorgeschlagen",
        "storniert": "storniert",
    }.get(conv.state, conv.state)

    text = (
        f"📬 <b>Folge-Mail vom Kunden</b> ({_h(state_label)})\n"
        f"<b>Von:</b> {_h(sender_name)} ({_h(sender_email)})\n"
        f"<b>Betreff:</b> {_h(subject[:80])}\n"
        f"<b>Preview:</b> {_h(body_preview[:300])}\n"
        f"<i>(keine Auto-Reply versendet — Mail steht im Outlook)</i>"
    )

    try:
        from plugins.telegram_notify.handler import TelegramNotifier
        ok = await TelegramNotifier.send_for_tenant(
            tenant.id, text, employee_id=target_employee_id,
        )
        return bool(ok)
    except Exception as e:
        logger.warning(
            f"push_tenant_followup_mail tenant={tenant.slug} "
            f"kunde={sender_email}: {e}"
        )
        return False
