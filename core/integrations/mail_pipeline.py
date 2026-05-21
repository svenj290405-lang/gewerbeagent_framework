"""
Mail-Pipeline Utilities fuer die Microsoft-Pipeline.

Kapselt EmailConversation-Lookups, State-Uebergaenge und Tenant-
Telegram-Pushes — die Bausteine fuer Reply-Threading + Follow-up-
Routing in core/integrations/microsoft_inbox.py.

Historie: dieses Modul ersetzt die fruehere Brevo-Inbound-Pipeline
(plugins/mail_intake/, entfernt im Mail-Pipeline-Refactor). Die
nuetzlichen Helper (find/upsert_conversation, Storno-Resolver,
Slot-Helper, Tenant-Push) wurden hier neu implementiert — nicht 1:1
portiert — damit sie sauber auf Microsoft Graph aufsetzen statt auf
Brevos REST-API.

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

# Wie lange darf der dritte (email-only) Fallback eine alte Konversation
# als "noch offen" behandeln? Aelter -> als geschlossen werten und
# stattdessen Neu-Anfrage starten. Verhindert dass eine Mail zu einem
# voellig neuen Thema in eine Wochen-alte Test-Konv gerouted wird.
EMAIL_FALLBACK_MAX_AGE_DAYS = 7


def _subject_token_overlap(a: str | None, b: str | None) -> bool:
    """Sehr lockerer Subject-Match: gibt es ein gemeinsames signifikantes
    Wort? Ignoriert "Re:", "Fwd:", Whitespace, Kurzwoerter <4 Zeichen.

    Wird im email-only Fallback genutzt: nur wenn das neue Subject
    irgendeinen thematischen Bezug zum letzten Subject hat, gilt die
    Konv noch als "selber Vorgang". Sonst -> Neu-Anfrage.
    """
    import re as _re
    def _tokens(s: str | None) -> set[str]:
        if not s:
            return set()
        # Re:/Fwd:/Aw: am Anfang strippen, dann auf Tokens splitten
        s = _re.sub(r"^\s*((re|fwd|aw|wg)\s*:\s*)+", "", s, flags=_re.IGNORECASE)
        return {
            t for t in _re.findall(r"\w+", s.lower())
            if len(t) >= 4
        }
    return bool(_tokens(a) & _tokens(b))


async def find_open_conversation(
    tenant_id: uuid.UUID,
    sender_email: str,
    *,
    microsoft_conversation_id: str | None = None,
    in_reply_to: str | None = None,
    current_subject: str | None = None,
) -> EmailConversation | None:
    """Sucht eine bestehende OFFENE Konversation (state != CLOSED).

    Match-Reihenfolge:
      1. ms_conv_id — provider-natives Threading
      2. in_reply_to — RFC In-Reply-To gegen unsere letzte Q-Mail-ID
      3. email-only Fallback MIT Constraints:
         - updated_at innerhalb EMAIL_FALLBACK_MAX_AGE_DAYS (7d)
         - Subject-Token-Bezug zum letzten Subject ODER kein
           current_subject angegeben (fuer Legacy-Caller)
         Sonst gilt die alte Konv als "nicht mehr derselbe Vorgang"
         und wir starten eine Neu-Anfrage. Verhindert dass alte
         Test-Konversationen neue Themen vom selben Absender kapern.

    Returns: EmailConversation (expunged aus der Session) oder None.
    """
    sender_email_norm = (sender_email or "").strip().lower()

    async with AsyncSessionLocal() as s:
        # 1. Microsoft conversationId match — provider-natives Threading
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

        # 3. Fallback per kunde_email — mit Time-Window + Subject-Bezug
        if sender_email_norm:
            import datetime as _dt
            cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
                days=EMAIL_FALLBACK_MAX_AGE_DAYS,
            )
            r = await s.execute(
                select(EmailConversation)
                .where(
                    EmailConversation.tenant_id == tenant_id,
                    EmailConversation.kunde_email == sender_email_norm,
                    EmailConversation.state != STATE_CLOSED,
                    EmailConversation.updated_at >= cutoff,
                )
                .order_by(EmailConversation.updated_at.desc())
                .limit(1)
            )
            conv = r.scalar_one_or_none()
            if conv:
                # Subject-Bezug pruefen — wenn der Caller current_subject
                # mitgibt und keine gemeinsamen Worte mit dem letzten
                # Subject existieren, gilt die alte Konv als nicht
                # mehr "derselbe Vorgang".
                if current_subject is not None and not _subject_token_overlap(
                    conv.last_subject, current_subject,
                ):
                    logger.info(
                        f"find_open_conversation: email-fallback "
                        f"tenant={tenant_id} kunde={sender_email_norm} "
                        f"verworfen — subject-mismatch "
                        f"alt={(conv.last_subject or '')[:40]!r} "
                        f"neu={(current_subject or '')[:40]!r} "
                        f"-> Neu-Anfrage"
                    )
                    return None
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
    gcal_event_id: str | None = None,
    termin_datum=None,  # datetime.date oder None
    state: str | None = None,
) -> EmailConversation:
    """Legt eine neue Konversation an.

    Default-state: AWAITING_CONFIRMATION (Mail-Pipeline-Eingang). Voice-
    Booking-Pfad uebergibt state=BOOKED + gcal_event_id + termin_datum.

    Wird vom Microsoft-Inbox-Handler aufgerufen wenn KEIN bestehender
    Thread gefunden wurde, und vom Voice-Buchung-Handler (Teil E) um
    eine telefonisch entstandene Konversation an die Mail-Adresse zu
    haengen — damit spaetere Folge-Mails (Storno-Antwort, Frage zum
    Termin) zur richtigen Konv. gematcht werden.

    Returns: persistierte EmailConversation (expunged).
    """
    async with AsyncSessionLocal() as s:
        conv = EmailConversation(
            tenant_id=tenant_id,
            kunde_email=(sender_email or "").strip().lower(),
            kunde_name=(sender_name or None),
            last_subject=(subject or None) and subject[:500],
            microsoft_conversation_id=microsoft_conversation_id,
            state=state or STATE_AWAITING_CONFIRMATION,
            assigned_employee_id=assigned_employee_id,
            gcal_event_id=gcal_event_id,
            termin_datum=termin_datum,
        )
        s.add(conv)
        await s.commit()
        await s.refresh(conv)
        s.expunge(conv)
    logger.info(
        f"mail_pipeline: neue Konversation angelegt id={conv.id} "
        f"tenant={tenant_id} kunde={conv.kunde_email} "
        f"state={conv.state} ms_conv_id={(microsoft_conversation_id or '')[:30]} "
        f"event_id={(gcal_event_id or '')[:20]}"
    )
    return conv


async def find_conversation_by_outbound_message_id(
    tenant_id: uuid.UUID, outbound_message_id: str,
) -> EmailConversation | None:
    """Sucht eine Konversation anhand der internetMessageId der zuletzt
    versendeten Q-Reply (= EmailConversation.last_message_id).

    Wird vom Bounce-Handler (Teil G) gerufen: die bounce-Mail hat als
    In-Reply-To die Message-ID unserer Q-Antwort. Wenn wir die Konv.
    finden, koennen wir state=STATE_DELIVERY_FAILED setzen und den MA
    informieren dass seine/Q's Antwort nicht angekommen ist.

    Bewusst KEIN Filter auf state — auch bei Re-Bounce einer
    bereits-bounced Konv. wollen wir die finden (z.B. um den Push zu
    wiederholen oder das classification_reason zu erweitern).
    """
    if not outbound_message_id:
        return None
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(EmailConversation)
            .where(
                EmailConversation.tenant_id == tenant_id,
                EmailConversation.last_message_id == outbound_message_id,
            )
            .order_by(EmailConversation.updated_at.desc())
            .limit(1)
        )
        conv = r.scalar_one_or_none()
        if conv:
            s.expunge(conv)
        return conv


async def find_conversation_by_event_id(
    tenant_id: uuid.UUID, gcal_event_id: str,
) -> EmailConversation | None:
    """Sucht eine Konversation anhand der Kalender-event_id.

    Wird vom Voice-Storno-Handler (Teil E.2) aufgerufen: nach erfolg-
    reichem cancel_appointment haben wir die event_id, aber nicht
    direkt die Kunden-Mail. Wenn der Termin urspruenglich ueber das
    Voice-Booking-Setup angelegt wurde (Teil E.1), existiert eine
    Konversation mit gcal_event_id=diese ID, und wir koennen die
    Storno-Bestaetigungs-Mail an conv.kunde_email schicken.

    Bei voice-only Kunden ohne Mail-Adresse zur Buchzeit gibt es
    keine Konversation — None ist dann normal.
    """
    if not gcal_event_id:
        return None
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(EmailConversation)
            .where(
                EmailConversation.tenant_id == tenant_id,
                EmailConversation.gcal_event_id == gcal_event_id,
            )
            .order_by(EmailConversation.updated_at.desc())
            .limit(1)
        )
        conv = r.scalar_one_or_none()
        if conv:
            s.expunge(conv)
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


async def set_conversation_state(
    conv_id: uuid.UUID, state: str,
) -> None:
    """Setzt nur den state-Wert einer Konversation.

    Wird vom Storno-Handler genutzt um STATE_STORNIERT zu vermerken
    nachdem Termine geloescht und Bestaetigungs-Mail raus ist.
    Trennscharf von record_inbound (Klassifikations-Update) und
    mark_delivery_failed (Bounce-spezifisch).
    """
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(EmailConversation).where(EmailConversation.id == conv_id)
        )
        conv = r.scalar_one_or_none()
        if not conv:
            logger.warning(
                f"set_conversation_state: Konversation {conv_id} nicht gefunden"
            )
            return
        conv.state = state[:50]
        await s.commit()


async def set_proposed_slots(
    conv_id: uuid.UUID,
    slots: list[dict] | None,
    *,
    state: str | None = None,
) -> None:
    """Persistiert die Liste vorgeschlagener Slots an der Konversation.

    Wird im PROPOSE_SLOTS-Pfad des Mail-Dialogs aufgerufen: nach der
    Find-Free-Slots-Antwort vom kalender-Plugin schreiben wir die
    Vorschlaege in conv.proposed_slots, damit Q im naechsten Turn
    referenzieren kann ("ja der zweite passt" -> chosen_slot_index=1).

    state: optional gleichzeitig state mitschreiben (typisch
    STATE_PROPOSING_SLOTS) — spart eine zweite Transaktion.

    slots=None loescht die alten Vorschlaege (z.B. nach erfolgreicher
    Buchung, damit ein spaeterer BOOK_SLOT nicht auf abgelaufene
    Slots zugreift).
    """
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(EmailConversation).where(EmailConversation.id == conv_id)
        )
        conv = r.scalar_one_or_none()
        if not conv:
            logger.warning(
                f"set_proposed_slots: Konversation {conv_id} nicht gefunden"
            )
            return
        conv.proposed_slots = list(slots) if slots else None
        if state:
            conv.state = state[:50]
        await s.commit()


async def set_conversation_drive_url(
    conv_id: uuid.UUID, drive_folder_url: str,
) -> None:
    """Vermerkt den Kunden-Drive-Ordner-Link an der Konversation.

    Gesetzt nach Formular-Eingang (Drive-Archiv). Beim Termin-Buchen
    liest der Pipeline-Pfad das Feld und schreibt den Link in die
    Kalender-Event-Beschreibung.
    """
    async with AsyncSessionLocal() as s:
        r = await s.execute(
            select(EmailConversation).where(EmailConversation.id == conv_id)
        )
        conv = r.scalar_one_or_none()
        if not conv:
            logger.warning(
                f"set_conversation_drive_url: Konversation {conv_id} nicht gefunden"
            )
            return
        conv.drive_folder_url = drive_folder_url[:1000]
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


# ====================================================================
# Intent-Handler — Storno / Verschiebung / Rechnungsanfrage (Teil D.2)
# ====================================================================
#
# Diese Funktionen kapseln die Microsoft-spezifische Storno-/Verschie-
# bungs-Logik. Sie werden vom poll-Loop in microsoft_inbox.py aufgerufen,
# nachdem das Intent (siehe core.ai.gemini) bestimmt wurde.
#
# Versand: Microsoft Graph (send_tracked_mail). Persistenz: mail_pipeline-
# create/record-Helper. Kalender: plugin-system (kalender.on_webhook).

async def cancel_kunde_termine(
    tenant: Tenant,
    kunde_email: str,
    existing_conv: EmailConversation | None,
) -> list[str]:
    """Findet + storniert alle Termine eines Kunden.

    Strategie (1:1 portiert aus _resolve_and_cancel_storno_events
    im Brevo-Handler):
      1. kalender.find_events(kunde_email=...) — strukturierte Metadaten
         + Volltext-Fallback ueber alle Mitarbeiter-Kalender
      2. Fallback: existing_conv.gcal_event_id (fuer Legacy-Termine ohne
         Mail-Metadaten/-Description)

    Returns: Liste der tatsaechlich stornierten event_ids (kann leer
    sein wenn weder find noch fallback was hatte — Caller entscheidet
    dann ob eine Rueckfrage-Mail noetig ist).
    """
    from core.plugin_system import get_plugin_for_tenant

    kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
    if kalender is None:
        logger.warning(
            f"cancel_kunde_termine: tenant={tenant.slug} hat keinen "
            f"Kalender-Adapter — Storno-Mail kann nicht verarbeitet werden"
        )
        return []

    cancelled: list[str] = []
    seen: set[str] = set()

    try:
        find_res = await kalender.on_webhook(
            "find_events", {"kunde_email": kunde_email.lower()},
        )
    except Exception as e:
        logger.warning(
            f"cancel_kunde_termine: find_events failed tenant={tenant.slug} "
            f"kunde={kunde_email}: {e}"
        )
        find_res = {"erfolg": False, "termine": []}

    if find_res.get("erfolg"):
        for ev in find_res.get("termine", []):
            eid = ev.get("event_id")
            if not eid or eid in seen:
                continue
            seen.add(eid)
            try:
                await kalender.on_webhook(
                    "cancel_appointment", {"event_id": eid},
                )
                cancelled.append(eid)
            except Exception as e:
                logger.warning(
                    f"cancel_kunde_termine: cancel event_id={eid} "
                    f"tenant={tenant.slug}: {e}"
                )

    # Legacy-Fallback: conv.gcal_event_id falls find nichts hatte
    conv_event_id = (
        existing_conv.gcal_event_id if existing_conv is not None else None
    )
    if conv_event_id and conv_event_id not in seen:
        try:
            await kalender.on_webhook(
                "cancel_appointment", {"event_id": conv_event_id},
            )
            cancelled.append(conv_event_id)
        except Exception as e:
            logger.warning(
                f"cancel_kunde_termine: legacy-fallback cancel "
                f"event_id={conv_event_id} tenant={tenant.slug}: {e}"
            )

    logger.info(
        f"cancel_kunde_termine: tenant={tenant.slug} kunde={kunde_email} "
        f"stornier-events={len(cancelled)} (find={len(seen)}, "
        f"fallback={'1' if conv_event_id and conv_event_id not in seen else '0'})"
    )
    return cancelled


def _build_storno_html(
    *, kunde_anrede: str, company_name: str,
    cancelled_count: int, original_subject: str,
) -> str:
    """Sehr knappe Storno-Bestaetigungs-Mail. Ton: bestaetigend, ohne
    Marketing-Floskeln. Verweis auf neuen Termin-Wunsch als optionales
    Add-on, kein Druck.
    """
    from html import escape as _h

    if cancelled_count == 0:
        # Sonderfall: wir haben nichts gefunden zum Stornieren.
        # Hoeflich rueckfragen.
        body = (
            f"<p>danke für die Nachricht. Wir können aktuell keinen "
            f"bestehenden Termin auf Ihre Adresse finden. Falls Sie einen "
            f"konkreten Termin meinen, antworten Sie bitte kurz mit Datum + "
            f"Uhrzeit — dann prüfen wir das nochmal manuell.</p>"
        )
    else:
        storno_satz = (
            "Ihr Termin wurde storniert." if cancelled_count == 1
            else f"Ihre {cancelled_count} Termine wurden storniert."
        )
        body = (
            f"<p>{storno_satz} Sie erhalten dafür keine Rechnung.</p>"
            f"<p>Falls Sie einen neuen Termin möchten, antworten Sie "
            f"einfach auf diese Mail oder rufen Sie uns an.</p>"
        )

    anrede = f"Hallo {_h(kunde_anrede)}," if kunde_anrede else "Hallo,"
    return (
        f"<html><body style=\"font-family:Arial,Helvetica,sans-serif;"
        f"font-size:14px;color:#222\">"
        f"<p>{anrede}</p>"
        f"{body}"
        f"<p>Viele Grüße,<br>{_h(company_name)}</p>"
        f"</body></html>"
    )


def _build_verschiebung_html(
    *, kunde_anrede: str, company_name: str,
    found_termine: list[dict],
) -> str:
    """Hoefliche Rueckfrage: wir haben den Termin gefunden, bitte
    Wunsch nennen. Wenn KEIN Termin gefunden wurde, Rueckfrage statt
    Vorschlag.
    """
    from html import escape as _h

    if not found_termine:
        body = (
            f"<p>danke für Ihre Nachricht. Wir können aktuell keinen "
            f"bestehenden Termin auf Ihre Adresse finden. Bitte antworten "
            f"Sie kurz mit Ihrem aktuellen Termin (Datum + Uhrzeit) und "
            f"Ihrem Wunsch-Ersatztermin, dann setzen wir das um.</p>"
        )
    else:
        # Ersten Treffer im Klartext anteasern damit der Kunde sich
        # bestaetigt fuehlt dass wir den richtigen Termin meinen.
        ev = found_termine[0]
        datum = ev.get("datum", "")
        uhrzeit = ev.get("uhrzeit", "")
        termin_label = (
            f"am {_h(datum)} um {_h(uhrzeit)} Uhr"
            if datum or uhrzeit else "in unserem Kalender"
        )
        body = (
            f"<p>danke für Ihre Nachricht. Wir haben Ihren Termin "
            f"{termin_label} gefunden.</p>"
            f"<p>Bitte antworten Sie kurz mit Ihrem Wunsch-Ersatztermin "
            f"(z.B. \"Donnerstag 14:00\") oder rufen Sie uns an — dann "
            f"buchen wir um.</p>"
        )

    anrede = f"Hallo {_h(kunde_anrede)}," if kunde_anrede else "Hallo,"
    return (
        f"<html><body style=\"font-family:Arial,Helvetica,sans-serif;"
        f"font-size:14px;color:#222\">"
        f"<p>{anrede}</p>"
        f"{body}"
        f"<p>Viele Grüße,<br>{_h(company_name)}</p>"
        f"</body></html>"
    )


def _build_storno_text(
    *, kunde_anrede: str, company_name: str, cancelled_count: int,
) -> str:
    """Plain-Text-Variante der Storno-Bestaetigung (multipart/alternative)."""
    anrede = f"Hallo {kunde_anrede}," if kunde_anrede else "Hallo,"
    if cancelled_count == 0:
        body = (
            "danke für die Nachricht. Wir können aktuell keinen bestehenden "
            "Termin auf Ihre Adresse finden. Falls Sie einen konkreten Termin "
            "meinen, antworten Sie bitte kurz mit Datum + Uhrzeit — dann "
            "prüfen wir das nochmal manuell."
        )
    else:
        storno_satz = (
            "Ihr Termin wurde storniert." if cancelled_count == 1
            else f"Ihre {cancelled_count} Termine wurden storniert."
        )
        body = (
            f"{storno_satz} Sie erhalten dafür keine Rechnung.\n\nFalls Sie "
            "einen neuen Termin möchten, antworten Sie einfach auf diese Mail "
            "oder rufen Sie uns an."
        )
    return f"{anrede}\n\n{body}\n\nViele Grüße,\n{company_name}\n"


def _build_verschiebung_text(
    *, kunde_anrede: str, company_name: str, found_termine: list[dict],
) -> str:
    """Plain-Text-Variante der Verschiebungs-Rueckfrage."""
    anrede = f"Hallo {kunde_anrede}," if kunde_anrede else "Hallo,"
    if not found_termine:
        body = (
            "danke für Ihre Nachricht. Wir können aktuell keinen bestehenden "
            "Termin auf Ihre Adresse finden. Bitte antworten Sie kurz mit "
            "Ihrem aktuellen Termin (Datum + Uhrzeit) und Ihrem Wunsch-"
            "Ersatztermin, dann setzen wir das um."
        )
    else:
        ev = found_termine[0]
        datum = ev.get("datum", "")
        uhrzeit = ev.get("uhrzeit", "")
        termin_label = (
            f"am {datum} um {uhrzeit} Uhr" if (datum or uhrzeit)
            else "in unserem Kalender"
        )
        body = (
            f"danke für Ihre Nachricht. Wir haben Ihren Termin {termin_label} "
            "gefunden.\n\nBitte antworten Sie kurz mit Ihrem Wunsch-Ersatztermin "
            "(z.B. \"Donnerstag 14:00\") oder rufen Sie uns an — dann buchen "
            "wir um."
        )
    return f"{anrede}\n\n{body}\n\nViele Grüße,\n{company_name}\n"


async def send_storno_confirmation(
    *, tenant_id: uuid.UUID,
    to_email: str, kunde_anrede: str, company_name: str,
    original_subject: str, cancelled_count: int,
    employee_id: uuid.UUID | None = None,
) -> dict:
    """Versendet die Storno-Bestaetigung via send_tracked_mail.

    Returns: sent_meta-Dict {success, message_id, internet_message_id,
    conversation_id, error}. Caller persistiert das in
    record_outbound_q_reply.
    """
    from core.integrations.microsoft import send_tracked_mail

    body_html = _build_storno_html(
        kunde_anrede=kunde_anrede,
        company_name=company_name,
        cancelled_count=cancelled_count,
        original_subject=original_subject,
    )
    body_text = _build_storno_text(
        kunde_anrede=kunde_anrede,
        company_name=company_name,
        cancelled_count=cancelled_count,
    )
    reply_subject = (
        f"Re: {original_subject}"
        if not (original_subject or "").lower().startswith("re:")
        else original_subject
    )
    return await send_tracked_mail(
        tenant_id=tenant_id,
        to_email=to_email,
        subject=reply_subject,
        body_html=body_html,
        body_text=body_text,
        employee_id=employee_id,
    )


async def send_storno_confirmation_for_event(
    *,
    tenant_id: uuid.UUID,
    company_name: str,
    event_id: str,
    employee_id: uuid.UUID | None = None,
    cancelled_count: int = 1,
) -> bool:
    """Schickt die Storno-Bestaetigungs-Mail an den Kunden eines stornierten
    Termins — sofern sich seine Mail-Adresse aufloesen laesst.

    Die Kunden-Mail wird ueber die zur event_id gehoerende EmailConversation
    aufgeloest (gcal_event_id-Match). Termine, die rein telefonisch ohne
    Mail-Adresse gebucht wurden, haben keine Konversation -> dann wird KEINE
    Mail verschickt (Return False).

    Best-effort: faengt alle Fehler ab und loggt nur. Der Storno selbst ist
    zu diesem Zeitpunkt bereits durchgefuehrt; ein Mail-Fehler darf den
    Aufrufer nie blockieren. Wiederverwendbar fuer alle Storno-Eintrittspunkte
    (z.B. /storno-Telegram-Wizard). Returns True, wenn eine Mail rausging.
    """
    from core.integrations.mail_template import extract_first_name
    from core.models import STATE_STORNIERT

    try:
        conv = await find_conversation_by_event_id(tenant_id, event_id)
        if conv is None or not conv.kunde_email:
            logger.info(
                f"storno-mail: keine EmailConversation mit Mailadresse fuer "
                f"event={event_id[:20]} — kein Mail-Versand"
            )
            return False

        kunde_anrede = extract_first_name(conv.kunde_name or "") or ""
        original_subject = conv.last_subject or "Ihre Terminbuchung"

        sent_meta = await send_storno_confirmation(
            tenant_id=tenant_id,
            to_email=conv.kunde_email,
            kunde_anrede=kunde_anrede,
            company_name=company_name or "",
            original_subject=original_subject,
            cancelled_count=cancelled_count,
            employee_id=employee_id,
        )
        if not sent_meta.get("success"):
            logger.warning(
                f"storno-mail: send fehlgeschlagen tenant={tenant_id} "
                f"kunde={conv.kunde_email}: {sent_meta.get('error')}"
            )
            return False

        reply_subject = (
            f"Re: {original_subject}"
            if not original_subject.lower().startswith("re:")
            else original_subject
        )
        await record_outbound_q_reply(
            conv.id,
            internet_message_id=sent_meta.get("internet_message_id"),
            microsoft_conversation_id=sent_meta.get("conversation_id"),
            q_reply_text=(
                f"[Storno-Bestaetigung: {cancelled_count} Termin(e) storniert]"
            ),
            subject=reply_subject,
        )
        await set_conversation_state(conv.id, STATE_STORNIERT)
        logger.info(
            f"storno-mail: gesendet tenant={tenant_id} "
            f"kunde={conv.kunde_email} conv_id={conv.id}"
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception(
            f"storno-mail: abgestuerzt (Storno steht trotzdem): {e}"
        )
        return False


async def send_verschiebung_request(
    *, tenant_id: uuid.UUID,
    to_email: str, kunde_anrede: str, company_name: str,
    original_subject: str, found_termine: list[dict],
    employee_id: uuid.UUID | None = None,
) -> dict:
    """Versendet die Verschiebungs-Rueckfrage. Returns sent_meta-Dict."""
    from core.integrations.microsoft import send_tracked_mail

    body_html = _build_verschiebung_html(
        kunde_anrede=kunde_anrede,
        company_name=company_name,
        found_termine=found_termine,
    )
    body_text = _build_verschiebung_text(
        kunde_anrede=kunde_anrede,
        company_name=company_name,
        found_termine=found_termine,
    )
    reply_subject = (
        f"Re: {original_subject}"
        if not (original_subject or "").lower().startswith("re:")
        else original_subject
    )
    return await send_tracked_mail(
        tenant_id=tenant_id,
        to_email=to_email,
        subject=reply_subject,
        body_html=body_html,
        body_text=body_text,
        employee_id=employee_id,
    )


# --------------------------------------------------------------------
# Dankes-Mail nach Formular-Eingang
# --------------------------------------------------------------------

async def send_formular_dank_mail(
    *,
    tenant_id: uuid.UUID,
    to_email: str,
    kunde_anrede: str,
    company_name: str,
    contact_name: str,
    contact_phone: str,
    original_subject: str | None,
    employee_id: uuid.UUID | None = None,
    termin_besteht: bool = False,
) -> dict:
    """Dankes-Mail nachdem der Kunde das Anfrage-Formular ausgefuellt hat.

    Bestaetigt den Eingang. Bewusst OHNE Formular-Button (Formular ist ja
    durch) — nutzt build_kunde_reply_html mit with_formular_button=False.

    termin_besteht: True, wenn fuer die Konversation bereits ein Termin
    gebucht ist (neuer Flow: erst Termin, dann Formular). Dann wird NICHT
    erneut nach einem Wunschtermin gefragt — nur bedankt + fuer Rueck-
    fragen offen. False (reiner Angebots-Fall ohne Termin): Kunde darf
    direkt einen Wunschtermin nennen, der ueber Reply-Threading in den
    Dialog-Pfad laeuft.

    Returns: sent_meta-Dict. Caller persistiert die Konversation.
    """
    from core.integrations.microsoft import send_tracked_mail
    from core.integrations.mail_template import (
        build_kunde_reply_html, build_kunde_reply_text,
    )

    if termin_besteht:
        reply_text = (
            "vielen Dank, deine Angaben sind bei mir angekommen — damit "
            "kann ich deinen Termin gut vorbereiten. Wenn du noch Fragen "
            "hast, antworte einfach auf diese Mail."
        )
    else:
        reply_text = (
            "vielen Dank, deine Angaben sind bei mir angekommen. "
            "Wenn du noch Fragen hast, antworte einfach auf diese Mail. "
            "Oder nenn mir direkt deinen Wunschtermin (z.B. \"Donnerstag "
            "14 Uhr\" oder \"nächste Woche vormittags\") — dann schaue ich "
            "nach einem passenden Termin für dich."
        )
    body_html = build_kunde_reply_html(
        kunde_anrede_name=kunde_anrede,
        kunde_email=to_email,
        reply_text=reply_text,
        form_url="",
        company_name=company_name,
        contact_name=contact_name,
        contact_phone=contact_phone,
        with_formular_button=False,
    )
    body_text = build_kunde_reply_text(
        kunde_anrede_name=kunde_anrede,
        reply_text=reply_text,
        form_url="",
        company_name=company_name,
        contact_name=contact_name,
        contact_phone=contact_phone,
        with_formular_button=False,
    )
    reply_subject = (
        f"Re: {original_subject}"
        if original_subject and not original_subject.lower().startswith("re:")
        else (original_subject or "Ihre Anfrage ist eingegangen")
    )
    return await send_tracked_mail(
        tenant_id=tenant_id,
        to_email=to_email,
        subject=reply_subject,
        body_html=body_html,
        body_text=body_text,
        employee_id=employee_id,
    )


# --------------------------------------------------------------------
# Voice-Booking-Confirmation (Teil E.1 + E.2)
# --------------------------------------------------------------------

def _build_buche_confirmation_html(
    *,
    kunde_anrede: str,
    company_name: str,
    datum_label: str,
    uhrzeit: str,
    employee_name: str | None,
    anliegen: str,
    contact_phone: str,
) -> str:
    """Bestaetigungs-Mail nach erfolgreichem Voice-Booking.

    Bewusst KEIN Storno-Link mit Token — der Kunde antwortet einfach
    auf die Mail mit "absagen" oder "verschieben", die Microsoft-
    Pipeline (Teil D Intent-Erkennung) catched das automatisch und
    triggert die Storno-/Verschiebungs-Handler. Eine extra Token-URL
    waere doppelte Infrastruktur ohne Mehrwert.
    """
    from html import escape as _h

    anrede = f"Hallo {_h(kunde_anrede)}," if kunde_anrede else "Hallo,"
    durch_wen = (
        f"<p>{_h(employee_name)} kommt am vereinbarten Termin.</p>"
        if employee_name else ""
    )
    phone_line = (
        f'<p>Rueckruf-Nummer: <a href="tel:{_h(contact_phone)}">'
        f'{_h(contact_phone)}</a></p>'
        if contact_phone else ""
    )
    return (
        f'<html><body style="font-family:Arial,Helvetica,sans-serif;'
        f'font-size:14px;color:#222">'
        f"<p>{anrede}</p>"
        f"<p>vielen Dank für Ihren Anruf. Wir haben Ihren Termin "
        f"eingetragen:</p>"
        f"<p>"
        f"<b>Termin:</b> {_h(datum_label)} um {_h(uhrzeit)} Uhr<br>"
        f"<b>Anliegen:</b> {_h(anliegen)}"
        f"</p>"
        f"{durch_wen}"
        f"<p>Falls Sie den Termin verschieben oder absagen möchten, "
        f"antworten Sie einfach auf diese Mail — wir kümmern uns "
        f"drum.</p>"
        f"{phone_line}"
        f"<p>Viele Grüße,<br>{_h(company_name)}</p>"
        f"</body></html>"
    )


def _build_buche_confirmation_text(
    *,
    kunde_anrede: str,
    company_name: str,
    datum_label: str,
    uhrzeit: str,
    employee_name: str | None,
    anliegen: str,
    contact_phone: str,
) -> str:
    """Plain-Text-Variante der Voice-Buchungs-Bestaetigung."""
    anrede = f"Hallo {kunde_anrede}," if kunde_anrede else "Hallo,"
    lines = [
        anrede, "",
        "vielen Dank für Ihren Anruf. Wir haben Ihren Termin eingetragen:",
        "",
        f"Termin: {datum_label} um {uhrzeit} Uhr",
        f"Anliegen: {anliegen}",
    ]
    if employee_name:
        lines.append(f"{employee_name} kommt am vereinbarten Termin.")
    lines += [
        "",
        "Falls Sie den Termin verschieben oder absagen möchten, antworten "
        "Sie einfach auf diese Mail — wir kümmern uns drum.",
    ]
    if contact_phone:
        lines += ["", f"Rückruf-Nummer: {contact_phone}"]
    lines += ["", f"Viele Grüße,\n{company_name}"]
    return "\n".join(lines).strip() + "\n"


async def send_buche_confirmation(
    *,
    tenant_id: uuid.UUID,
    to_email: str,
    kunde_anrede: str,
    company_name: str,
    datum_label: str,
    uhrzeit: str,
    employee_name: str | None,
    anliegen: str,
    contact_phone: str,
    employee_id: uuid.UUID | None = None,
) -> dict:
    """Versendet Buchungs-Bestaetigung via send_tracked_mail aus dem
    Postfach des zustaendigen Mitarbeiters (oder Tenant-Default).

    Returns: sent_meta-Dict {success, message_id, internet_message_id,
    conversation_id, error}. Caller persistiert das in
    record_outbound_q_reply (E.3 Threading).
    """
    from core.integrations.microsoft import send_tracked_mail

    body_html = _build_buche_confirmation_html(
        kunde_anrede=kunde_anrede,
        company_name=company_name,
        datum_label=datum_label,
        uhrzeit=uhrzeit,
        employee_name=employee_name,
        anliegen=anliegen,
        contact_phone=contact_phone,
    )
    body_text = _build_buche_confirmation_text(
        kunde_anrede=kunde_anrede,
        company_name=company_name,
        datum_label=datum_label,
        uhrzeit=uhrzeit,
        employee_name=employee_name,
        anliegen=anliegen,
        contact_phone=contact_phone,
    )
    subject = f"Ihre Terminbestätigung — {datum_label} um {uhrzeit} Uhr"
    return await send_tracked_mail(
        tenant_id=tenant_id,
        to_email=to_email,
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        employee_id=employee_id,
    )


async def push_tenant_bounce_notification(
    tenant: Tenant,
    *,
    conv: EmailConversation,
    bounce_sender: str,
    bounce_reason: str,
    employee_id: uuid.UUID | None = None,
) -> bool:
    """Telegram-Push wenn unsere Q-Antwort gebounced ist (Teil G).

    Format ist bewusst alarmierend (⚠️) — fuer den MA ist das
    Action-Item: er sollte die Mail manuell nochmal versenden, die
    Adresse korrigieren oder dem Kunden anders zurueckmelden.

    Schickt an den der Konversation zugewiesenen Mitarbeiter falls
    moeglich, sonst an den Tenant-Default.

    Returns: True wenn Push abgeschickt, False bei Fehler.
    """
    from html import escape as _h

    target_employee_id = employee_id or conv.assigned_employee_id

    # Bounce-Reason auf 200 Zeichen begrenzen — DSN-Reports koennen
    # mehrere Seiten Text enthalten.
    reason_short = (bounce_reason or "")[:200]

    text = (
        f"⚠️ <b>Mail-Zustellung fehlgeschlagen</b>\n"
        f"<b>An:</b> {_h(conv.kunde_email)}\n"
        f"<b>Subject war:</b> {_h((conv.last_subject or '')[:80])}\n"
        f"<b>Bounce von:</b> {_h(bounce_sender)}\n"
        f"<b>Grund:</b> {_h(reason_short)}\n"
        f"<i>Bitte manuell pruefen — die Antwort an den Kunden kam "
        f"nicht an.</i>"
    )

    try:
        from plugins.telegram_notify.handler import TelegramNotifier
        ok = await TelegramNotifier.send_for_tenant(
            tenant.id, text, employee_id=target_employee_id,
        )
        return bool(ok)
    except Exception as e:
        logger.warning(
            f"push_tenant_bounce_notification tenant={tenant.slug} "
            f"kunde={conv.kunde_email}: {e}"
        )
        return False


async def push_tenant_new_anfrage_notification(
    tenant: Tenant,
    *,
    sender_email: str,
    sender_name: str,
    subject: str,
    body_preview: str,
    web_link: str | None = None,
    anfrage_url: str | None = None,
    employee_id: uuid.UUID | None = None,
) -> bool:
    """Telegram-Push an MA bei neuer RELEVANT_KUNDE-Anfrage (Teil F.1).

    Schickt eine strukturierte Notification mit:
      - 📧 Header + Sender-Name/-Mail
      - Subject + Preview (max 200 Zeichen body_preview pro Spec)
      - Klickbarer "Im Outlook oeffnen"-Link (Microsoft Graph webLink)
      - Optional Anfrage-Formular-URL falls verfuegbar (Q hat ja schon
        einen Formular-Link in seiner Antwort verschickt, fuer den MA
        ist es trotzdem hilfreich den direkten Link zu sehen)

    Telegram inline-keyboards werden NICHT benutzt — HTML-<a>-Tags im
    Message-Body sind unter parse_mode=HTML komplett ausreichend und
    benoetigen kein reply_markup-Plumbing.

    Bewusst getrennt von push_tenant_followup_mail (Teil C) — Neuanfrage
    vs Folge-Mail haben sehr unterschiedliche UX-Bedeutung fuer den MA
    (Neuanfrage = "Kunde gewonnen!", Folge = "Bitte schauen, evtl. handeln").

    Returns: True wenn Push abgeschickt, False bei Fehler.
    """
    from html import escape as _h

    preview_short = (body_preview or "").strip()[:200]
    outlook_line = (
        f'<a href="{_h(web_link)}">🔗 Im Outlook oeffnen</a>\n'
        if web_link else ""
    )
    formular_line = (
        f'<a href="{_h(anfrage_url)}">📝 Formular-Link (an Kunde versendet)</a>\n'
        if anfrage_url else ""
    )

    text = (
        f"📧 <b>Neue Kundenanfrage</b>\n"
        f"<b>Von:</b> {_h(sender_name)} ({_h(sender_email)})\n"
        f"<b>Betreff:</b> {_h(subject[:80])}\n"
        f"<b>Preview:</b> {_h(preview_short)}\n"
        f"{outlook_line}"
        f"{formular_line}"
    )

    try:
        from plugins.telegram_notify.handler import TelegramNotifier
        ok = await TelegramNotifier.send_for_tenant(
            tenant.id, text, employee_id=employee_id,
        )
        return bool(ok)
    except Exception as e:
        logger.warning(
            f"push_tenant_new_anfrage_notification tenant={tenant.slug} "
            f"kunde={sender_email}: {e}"
        )
        return False


async def push_tenant_intent_event(
    tenant: Tenant,
    *,
    sender_email: str,
    sender_name: str,
    subject: str,
    body_preview: str,
    label: str,
    detail: str = "",
    employee_id: uuid.UUID | None = None,
) -> bool:
    """Generischer Tenant-Telegram-Push fuer Intent-Events (Storno
    verarbeitet, Verschiebung erkannt, Rechnungsanfrage eingegangen).

    label: kurzer Status-Header ("Storno verarbeitet" / "Verschiebung
    erkannt" / "Rechnungsanfrage").
    detail: optionaler Detail-Zusatz (z.B. "2 Termine storniert").
    """
    from html import escape as _h

    extra_line = f"<b>Details:</b> {_h(detail)}\n" if detail else ""
    text = (
        f"📧 <b>{_h(label)}</b>\n"
        f"<b>Von:</b> {_h(sender_name)} ({_h(sender_email)})\n"
        f"<b>Betreff:</b> {_h(subject[:80])}\n"
        f"<b>Preview:</b> {_h(body_preview[:300])}\n"
        f"{extra_line}"
    )

    try:
        from plugins.telegram_notify.handler import TelegramNotifier
        ok = await TelegramNotifier.send_for_tenant(
            tenant.id, text, employee_id=employee_id,
        )
        return bool(ok)
    except Exception as e:
        logger.warning(
            f"push_tenant_intent_event tenant={tenant.slug} "
            f"label={label!r} kunde={sender_email}: {e}"
        )
        return False
