"""Microsoft Graph Inbox-Polling: ungelesene Mails holen + klassifizieren.

Nutzt Mail.ReadWrite Permission. Workflow:
1. Hol ungelesene Mails via /me/messages (nur Header + Preview)
2. Klassifiziere jede via Gemini (Subject + Sender + bodyPreview)
3. Loggen, in DB speichern
4. Phase 2 (spaeter): vollen Body holen bei RELEVANT_KUNDE und Pipeline triggern
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select

from core.ai.gemini import classify_mail_subject
from core.database import AsyncSessionLocal
from core.integrations.mail_template import (
    build_kunde_reply_html,
    extract_first_name,
)
from core.integrations.microsoft import (
    GRAPH_API_BASE,
    MicrosoftNotConnectedError,
    get_microsoft_token,
)
from core.models import EmailConversation, Tenant

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Bounce / Auto-Reply Pre-Filter
# ----------------------------------------------------------------------
# Vor der Gemini-Klassifikation pruefen wir auf typische Bounce-Mails
# und Out-of-Office-Antworten. Diese werden NICHT klassifiziert (spart
# Tokens) und vor allem NICHT beantwortet (sonst Endlosschleife).
#
# Erkennung erfolgt drei-stufig (jede schon allein ist hinreichend):
#  1) Header `Auto-Submitted` != "no" oder `Precedence: bulk`/`auto_reply`
#  2) Sender `MAILER-DAEMON@`, `noreply@`, `bounce@`, `postmaster@`
#  3) Subject enthaelt Bounce/OOO-Pattern (delivery failure, out of office,
#     auto-reply, abwesenheitsnotiz, undeliverable, mail delivery, ...)

BOUNCE_SUBJECT_PATTERNS = (
    "delivery failure", "delivery status notification", "undeliverable",
    "mail delivery failed", "returned mail", "mail returned",
    "out of office", "out-of-office", "abwesenheitsnotiz",
    "automatic reply", "automatische antwort", "auto-reply", "autoreply",
    "vacation reply", "ferienabwesenheit", "ich bin abwesend",
    "non-livraison",
)

BOUNCE_SENDER_PREFIXES = (
    "mailer-daemon@", "postmaster@", "bounce@", "bounces@",
    "no-reply@", "noreply@", "do-not-reply@", "donotreply@",
)


def is_bounce_or_autoreply(msg: dict) -> tuple[bool, str]:
    """True + Grund wenn die Mail nicht beantwortet werden soll.

    msg: Microsoft-Graph-Message-Objekt (oder kompatibles dict).
    """
    # 1) Internet-Header pruefen
    headers_obj = msg.get("internetMessageHeaders") or []
    for h in headers_obj:
        name = (h.get("name") or "").lower()
        value = (h.get("value") or "").lower()
        if name == "auto-submitted" and value not in ("", "no"):
            return True, f"auto-submitted={value}"
        if name == "precedence" and value in ("bulk", "auto_reply", "list", "junk"):
            return True, f"precedence={value}"
        if name == "x-auto-response-suppress":
            return True, "x-auto-response-suppress gesetzt"
        if name == "x-autoreply" and value:
            return True, f"x-autoreply={value}"

    # 2) Sender-Adresse pruefen
    sender = ((msg.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    sender_lc = sender.lower()
    for prefix in BOUNCE_SENDER_PREFIXES:
        if sender_lc.startswith(prefix):
            return True, f"sender={sender_lc}"

    # 3) Subject-Pattern pruefen
    subject_lc = (msg.get("subject") or "").lower()
    for pat in BOUNCE_SUBJECT_PATTERNS:
        if pat in subject_lc:
            return True, f"subject-pattern={pat!r}"

    return False, ""


async def fetch_unread_messages(
    tenant_id: UUID, top: int = 25,
    employee_id: UUID | None = None,
) -> list[dict]:
    """Holt die letzten N ungelesenen Mails (Header + Preview, nicht voller Body).

    Phase 1 Multi-OAuth: optional employee_id — pollt das Postfach
    eines bestimmten Mitarbeiters (statt nur Tenant-Default).

    Returns: Liste von Mail-Dicts mit id, subject, from, bodyPreview, receivedDateTime, isRead
    """
    access_token = await get_microsoft_token(tenant_id, employee_id=employee_id)

    # Nur Felder holen die wir brauchen - bodyPreview ist max 255 Zeichen
    # Filter: ungelesen UND noch keine Q-Kategorie
    # Microsoft Graph $filter mit categories: "categories/any(c:c eq 'X')"
    # Wir wollen das Gegenteil: KEINE der Q-Kategorien
    q_filter_parts = [f"categories/any(c:c eq \'{cat}\')" for cat in ALL_Q_CATEGORIES]
    not_q_marked = "not (" + " or ".join(q_filter_parts) + ")"
    full_filter = f"isRead eq false and {not_q_marked}"

    # internetMessageId + conversationId zusaetzlich holen damit der
    # Reply-Threading-Lookup in mail_pipeline.find_open_conversation
    # ohne extra Graph-Call funktioniert. internetMessageHeaders (fuer
    # In-Reply-To als RFC-Fallback) wird NICHT hier mitgeholt — Graph
    # liefert die in der List-Variante haeufig nicht zuverlaessig; wir
    # ziehen sie bei Bedarf in fetch_full_message.
    params = {
        "$filter": full_filter,
        "$select": (
            "id,subject,from,bodyPreview,categories,receivedDateTime,isRead,"
            "internetMessageId,conversationId"
        ),
        "$orderby": "receivedDateTime desc",
        "$top": top,
    }

    # Bewusst NUR Inbox pollen, nicht /me/messages (das wuerde auch den
    # Gewerbeagent-Ordner einschliessen und bereits beantwortete Mails
    # erneut zurueckliefern - Loop-Risiko).
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{GRAPH_API_BASE}/me/mailFolders/inbox/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        if resp.status_code != 200:
            raise ValueError(
                f"Graph /me/mailFolders/inbox/messages fehlgeschlagen: "
                f"{resp.status_code} {resp.text[:300]}"
            )
        data = resp.json()
        return data.get("value", [])


async def mark_as_read(
    tenant_id: UUID, message_id: str,
    employee_id: UUID | None = None,
) -> bool:
    """Markiert eine Mail als gelesen via Graph API."""
    try:
        access_token = await get_microsoft_token(tenant_id, employee_id=employee_id)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{GRAPH_API_BASE}/me/messages/{message_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"isRead": True},
            )
            return resp.status_code in (200, 204)
    except Exception as e:
        logger.warning(f"mark_as_read fehler: {e}")
        return False


async def poll_microsoft_inbox(
    tenant_id: UUID, employee_id: UUID | None = None,
) -> dict:
    """Hauptfunktion: Hol ungelesene Mails fuer Tenant/Mitarbeiter,
    klassifiziere alle.

    Phase 1 Multi-OAuth: optional employee_id — bestimmt welches
    Postfach gepollt wird (jeder Mitarbeiter hat sein eigenes).

    Returns: {checked: N, classified: {RELEVANT_KUNDE: 3, NICHT_RELEVANT: 5, ...},
              messages: [{subject, sender, classification, confidence, reason}, ...]}
    """
    # Tenant laden fuer Kontext
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        tenant = result.scalar_one_or_none()
    if not tenant:
        return {"error": "Tenant nicht gefunden", "checked": 0}

    tenant_company = tenant.company_name or "Handwerksbetrieb"
    tenant_branche = getattr(tenant, "branche", None) or "Handwerk"

    try:
        messages = await fetch_unread_messages(
            tenant_id, top=25, employee_id=employee_id,
        )
    except MicrosoftNotConnectedError:
        return {"error": "Microsoft nicht verbunden", "checked": 0}
    except Exception as e:
        logger.exception(f"poll_microsoft_inbox fehler: {e}")
        return {"error": str(e), "checked": 0}

    logger.info(
        f"poll_microsoft_inbox: tenant={tenant.slug} {len(messages)} ungelesene Mails gefunden"
    )

    classified_counts: dict[str, int] = {}
    results: list[dict] = []

    for msg in messages:
        subject = msg.get("subject", "(kein Betreff)") or "(kein Betreff)"
        from_obj = msg.get("from", {}) or {}
        from_email_obj = from_obj.get("emailAddress", {}) or {}
        sender_email = from_email_obj.get("address", "") or "unbekannt"
        sender_name = from_email_obj.get("name", "") or sender_email
        body_preview = msg.get("bodyPreview", "") or ""

        # ---- PRE-FILTER 1: Bounce / Out-of-Office ----
        # Spart Gemini-Tokens UND verhindert Endlos-Schleifen wenn ein
        # OOO-Bot auf Q antwortet.
        is_bounce, bounce_reason = is_bounce_or_autoreply(msg)
        if is_bounce:
            logger.info(
                f"poll: skip bounce/auto-reply tenant={tenant.slug} "
                f"sender={sender_email} reason={bounce_reason}"
            )
            classified_counts["BOUNCE"] = classified_counts.get("BOUNCE", 0) + 1
            results.append({
                "message_id": msg.get("id"),
                "subject": subject,
                "sender": sender_email,
                "classification": "BOUNCE",
                "confidence": "high",
                "reason": bounce_reason,
                "skipped": True,
            })
            # Mail als gelesen markieren damit naechster Poll sie nicht erneut sieht
            try:
                await mark_as_read(tenant_id, msg.get("id"), employee_id=employee_id)
            except Exception as e:
                # Harmlos: Mail wird beim naechsten Poll erneut als BOUNCE
                # durchlaufen, der Pre-Filter ist billig (kein Gemini-Call).
                # Trotzdem loggen damit dauerhafte Permission-Issues sichtbar sind.
                logger.warning(
                    f"poll: mark_as_read fuer BOUNCE-Mail fehlgeschlagen "
                    f"tenant={tenant_id} msg_id={(msg.get('id') or '')[:30]} "
                    f"sender={sender_email}: {e}"
                )
            continue

        # ---- PRE-FILTER 2: Spam-Throttle pro Sender ----
        # Wenn derselbe Absender in 24h schon >= 10 Mails geschickt hat:
        # Klassifizieren und Kategorie setzen, aber NICHT auto-antworten.
        try:
            from core.integrations.mail_throttle import (
                count_recent_replies_to,
                MAX_REPLIES_PER_SENDER_PER_DAY,
            )
            recent_reply_count = await count_recent_replies_to(
                tenant_id=tenant_id, sender_email=sender_email,
                window_hours=24,
            )
            spam_throttled = recent_reply_count >= MAX_REPLIES_PER_SENDER_PER_DAY
        except Exception as e:
            logger.debug(f"spam-throttle Check failed (egal): {e}")
            spam_throttled = False

        # Klassifikation - Subject + Sender + Preview als Hilfe.
        # body_preview separat (statt in den Subject-String einzubetten)
        # damit classify_mail_subject das Keyword-Backup fuer Intent
        # darauf anwenden kann.
        try:
            cls_result = await classify_mail_subject(
                subject=subject,
                sender=sender_email,
                tenant_company=tenant_company,
                tenant_branche=tenant_branche,
                body_preview=body_preview,
            )
            classification = cls_result.get("classification") or "UNSICHER"
            confidence = cls_result.get("confidence") or "low"
            reason = cls_result.get("reason") or ""
            intent = cls_result.get("intent") or "sonstiges"
            # Erfolg — Failure-Window fuer diesen Tenant zuruecksetzen.
            try:
                from core.integrations.failure_counter import (
                    MAIL_CLASSIFY_FAILURES,
                )
                MAIL_CLASSIFY_FAILURES.reset(key=str(tenant_id))
            except Exception as e:
                # Reset ist Best-Effort housekeeping; wenn der Counter-Import
                # oder Reset fehlschlaegt, beeintraechtigt das nur die
                # naechste Sven-Alert-Berechnung. Debug-Level reicht.
                logger.debug(f"failure_counter.reset fehlgeschlagen: {e}")
        except Exception as e:
            logger.warning(f"Klassifikation fehler fuer msg {msg.get('id')}: {e}")
            classification = "UNSICHER"
            confidence = "low"
            reason = f"Fehler: {e}"
            intent = "sonstiges"
            # Failure-Counter: nach 3 Fehlern pro 24h → Sven-Alert.
            try:
                from core.integrations.failure_counter import (
                    MAIL_CLASSIFY_FAILURES,
                )
                should_alert, count = MAIL_CLASSIFY_FAILURES.record_failure(
                    key=str(tenant_id),
                    reason=f"{type(e).__name__}: {e}",
                )
                if should_alert:
                    from core.integrations.admin_alerts import (
                        notify_sven_admin_alert,
                    )
                    await notify_sven_admin_alert(
                        kind=f"mail_classify_dead.{tenant_id}",
                        message=(
                            f"⚠️ <b>Mail-Klassifikation faellt aus</b>\n\n"
                            f"Tenant: <code>{tenant.slug}</code>\n"
                            f"Failures in 24h: <b>{count}</b>\n"
                            f"Letzter Fehler: <code>{str(e)[:200]}</code>"
                        ),
                        details={
                            "tenant_id": str(tenant_id),
                            "failure_count": count,
                        },
                    )
            except Exception as exc:
                logger.debug(f"mail_classify_failure_counter ignored: {exc}")

        classified_counts[classification] = classified_counts.get(classification, 0) + 1

        # Reply-Threading: vor der Auto-Verarbeitung pruefen ob fuer
        # diesen Thread bereits eine offene Konversation existiert.
        # Lookup via Microsoft conversationId (provider-native Thread-
        # Gruppierung) + sender_email-Fallback. Wenn ja: KEIN Auto-Reply
        # mit Formular-Link (waere peinliche Wiederholung), sondern
        # Telegram-Push an den zustaendigen Mitarbeiter — der entscheidet
        # manuell oder die kommende Storno-/Verschiebungs-Erkennung
        # (Teil D) uebernimmt.
        existing_conv = None
        if classification == "RELEVANT_KUNDE":
            try:
                from core.integrations.mail_pipeline import (
                    find_open_conversation,
                )
                existing_conv = await find_open_conversation(
                    tenant_id=tenant_id,
                    sender_email=sender_email,
                    microsoft_conversation_id=msg.get("conversationId"),
                )
            except Exception as e:
                logger.warning(
                    f"poll: conv-lookup fehlgeschlagen sender={sender_email}: {e}"
                )

        # Auto-Verarbeitung NUR bei RELEVANT_KUNDE und nicht throttled.
        # Confidence-Gate: bei "low" eskalieren statt blind auto-antworten,
        # damit Q nicht auf falsch verstandene Mails halluziniert.
        #
        # Dispatch-Reihenfolge (Teil D.2):
        #   1. intent == termin_stornieren  → Storno-Handler (auch ohne
        #      bestehende Konversation: Kunde koennte telefonisch gebucht
        #      und jetzt erstmals gemailt haben)
        #   2. intent == termin_verschieben → Verschiebungs-Handler
        #   3. intent == rechnungsanfrage   → nur Telegram-Push
        #   4. existing_conv != None        → Folge-Mail (Teil C)
        #   5. spam_throttled               → Outlook-Kategorie, kein Reply
        #   6. confidence == low            → Outlook-Kategorie, kein Reply
        #   7. sonst (neu_anfrage default)  → process_relevant_kunde_mail
        process_result = None
        if classification == "RELEVANT_KUNDE" and intent == "termin_stornieren":
            try:
                process_result = await _handle_storno_intent(
                    tenant=tenant, tenant_id=tenant_id,
                    message_id=msg.get("id"),
                    sender_email=sender_email, sender_name=sender_name,
                    subject=subject, body_preview=body_preview,
                    existing_conv=existing_conv,
                    employee_id=employee_id,
                    ms_conversation_id=msg.get("conversationId"),
                    classification=classification, confidence=confidence,
                    reason=reason, categories=msg.get("categories") or [],
                )
            except Exception as e:
                logger.exception(
                    f"poll: Storno-Handler crashed sender={sender_email}: {e}"
                )
                process_result = {"success": False, "error": str(e)}
        elif classification == "RELEVANT_KUNDE" and intent == "termin_verschieben":
            try:
                process_result = await _handle_verschiebung_intent(
                    tenant=tenant, tenant_id=tenant_id,
                    message_id=msg.get("id"),
                    sender_email=sender_email, sender_name=sender_name,
                    subject=subject, body_preview=body_preview,
                    existing_conv=existing_conv,
                    employee_id=employee_id,
                    ms_conversation_id=msg.get("conversationId"),
                    classification=classification, confidence=confidence,
                    reason=reason, categories=msg.get("categories") or [],
                )
            except Exception as e:
                logger.exception(
                    f"poll: Verschiebungs-Handler crashed sender={sender_email}: {e}"
                )
                process_result = {"success": False, "error": str(e)}
        elif classification == "RELEVANT_KUNDE" and intent == "rechnungsanfrage":
            try:
                process_result = await _handle_rechnungsanfrage_intent(
                    tenant=tenant, tenant_id=tenant_id,
                    message_id=msg.get("id"),
                    sender_email=sender_email, sender_name=sender_name,
                    subject=subject, body_preview=body_preview,
                    existing_conv=existing_conv,
                    employee_id=employee_id,
                    ms_conversation_id=msg.get("conversationId"),
                    classification=classification, confidence=confidence,
                    reason=reason, categories=msg.get("categories") or [],
                )
            except Exception as e:
                logger.exception(
                    f"poll: Rechnungs-Handler crashed sender={sender_email}: {e}"
                )
                process_result = {"success": False, "error": str(e)}
        elif classification == "RELEVANT_KUNDE" and existing_conv is not None:
            # FOLGE-MAIL auf bestehenden Vorgang
            logger.info(
                f"poll: Folge-Mail erkannt sender={sender_email} "
                f"conv_id={existing_conv.id} state={existing_conv.state} "
                f"— kein Auto-Reply, Telegram-Push an MA"
            )
            try:
                from core.integrations.mail_pipeline import (
                    record_inbound, push_tenant_followup_mail,
                )
                await record_inbound(
                    existing_conv.id,
                    last_user_message=body_preview,
                    classification=classification,
                    classification_confidence=confidence,
                    classification_reason=reason,
                    microsoft_conversation_id=msg.get("conversationId"),
                )
                await push_tenant_followup_mail(
                    tenant=tenant,
                    sender_email=sender_email,
                    sender_name=sender_name,
                    subject=subject,
                    body_preview=body_preview,
                    conv=existing_conv,
                    employee_id=employee_id,
                )
                # Outlook-Kategorie setzen + mark-as-read damit naechster
                # Poll diese Mail nicht erneut anfasst.
                try:
                    target_category = Q_CATEGORY_BY_CLASSIFICATION.get(
                        "RELEVANT_KUNDE"
                    )
                    if target_category and msg.get("id"):
                        await set_message_categories(
                            tenant_id=tenant_id, message_id=msg.get("id"),
                            categories=(msg.get("categories") or [])
                            + [target_category],
                            employee_id=employee_id,
                        )
                except Exception as e:
                    logger.warning(
                        f"poll: Outlook-Kategorie auf Folge-Mail "
                        f"sender={sender_email}: {e}"
                    )
                try:
                    await mark_as_read(
                        tenant_id, msg.get("id"), employee_id=employee_id,
                    )
                except Exception as e:
                    logger.warning(
                        f"poll: mark_as_read auf Folge-Mail "
                        f"sender={sender_email}: {e}"
                    )
                process_result = {
                    "success": True, "skipped": False,
                    "reason": "followup-pushed",
                    "conv_id": str(existing_conv.id),
                }
            except Exception as e:
                logger.exception(
                    f"poll: Folge-Mail-Handling fehlgeschlagen "
                    f"sender={sender_email}: {e}"
                )
                process_result = {"success": False, "error": str(e)}
        elif classification == "RELEVANT_KUNDE":
            if spam_throttled:
                logger.warning(
                    f"poll: Spam-Throttle greift fuer {sender_email} "
                    f"(>= {recent_reply_count} Antworten in 24h) - keine Auto-Reply"
                )
                process_result = {
                    "success": False, "skipped": True, "reason": "spam-throttle",
                }
                # Trotzdem Outlook-Kategorie setzen damit der Tenant es manuell sieht
                try:
                    target_category = Q_CATEGORY_BY_CLASSIFICATION.get("UNSICHER")
                    if target_category and msg.get("id"):
                        await set_message_categories(
                            tenant_id=tenant_id, message_id=msg.get("id"),
                            categories=(msg.get("categories") or []) + [target_category],
                            employee_id=employee_id,
                        )
                except Exception as e:
                    # Kein Setzen → Mail bleibt ungelesen + un-kategorisiert →
                    # naechster Poll re-klassifiziert sie mit voller Gemini-Cost.
                    logger.warning(
                        f"poll: Outlook-Kategorie UNSICHER setzen fehlgeschlagen "
                        f"(spam-throttle-Pfad) tenant={tenant_id} "
                        f"sender={sender_email} msg_id={(msg.get('id') or '')[:30]}: "
                        f"{e} — Mail wird beim naechsten Poll erneut klassifiziert"
                    )
            elif confidence == "low":
                logger.info(
                    f"poll: Low-Confidence-RELEVANT_KUNDE (sender={sender_email}) - "
                    f"keine Auto-Action, Inhaber muss manuell schauen"
                )
                process_result = {
                    "success": False, "skipped": True, "reason": "low-confidence",
                }
                # Outlook-Kategorie UNSICHER setzen damit es nicht verloren geht
                try:
                    target_category = Q_CATEGORY_BY_CLASSIFICATION.get("UNSICHER")
                    if target_category and msg.get("id"):
                        await set_message_categories(
                            tenant_id=tenant_id, message_id=msg.get("id"),
                            categories=(msg.get("categories") or []) + [target_category],
                            employee_id=employee_id,
                        )
                except Exception as e:
                    # Kein Setzen → naechster Poll re-klassifiziert mit voller
                    # Gemini-Cost; zusaetzlich verliert der Inhaber den
                    # Unsicher-Hinweis im Outlook.
                    logger.warning(
                        f"poll: Outlook-Kategorie UNSICHER setzen fehlgeschlagen "
                        f"(low-confidence-Pfad) tenant={tenant_id} "
                        f"sender={sender_email} msg_id={(msg.get('id') or '')[:30]}: "
                        f"{e} — Mail wird beim naechsten Poll erneut klassifiziert"
                    )
            else:
                try:
                    process_result = await process_relevant_kunde_mail(
                        tenant_id=tenant_id,
                        message_id=msg.get("id"),
                        classification_result={
                            "classification": classification,
                            "confidence": confidence,
                            "reason": reason,
                        },
                        employee_id=employee_id,
                    )
                except Exception as e:
                    logger.exception(f"process_relevant_kunde_mail fehler: {e}")
                    process_result = {"success": False, "error": str(e)}
        else:
            # Andere Klassifikationen: Outlook-Kategorie setzen, Mail bleibt in Inbox
            target_category = Q_CATEGORY_BY_CLASSIFICATION.get(classification)
            if target_category and msg.get("id"):
                try:
                    # Bestehende Kategorien beibehalten + Q-Kategorie hinzufuegen
                    existing_cats = msg.get("categories") or []
                    new_cats = list(existing_cats) + [target_category]
                    await set_message_categories(
                        tenant_id=tenant_id,
                        message_id=msg.get("id"),
                        categories=new_cats,
                        employee_id=employee_id,
                    )
                except Exception as e:
                    logger.warning(f"Kategorie setzen fehler (non-fatal): {e}")

        results.append({
            "subject": subject[:80],
            "sender": sender_email,
            "sender_name": sender_name,
            "classification": classification,
            "confidence": confidence,
            "reason": reason[:150],
            "message_id": msg.get("id"),
            "received": msg.get("receivedDateTime"),
            "preview": body_preview[:120],
            "process_result": process_result,
        })

        logger.info(
            f"  Mail '{subject[:50]}' from {sender_email} -> {classification} ({confidence})"
        )

    return {
        "checked": len(messages),
        "classified": classified_counts,
        "messages": results,
        "tenant_slug": tenant.slug,
        "polled_at": datetime.now(timezone.utc).isoformat(),
    }


# =====================================================================
# Vollen Mail-Body holen (fuer Reply-Generierung)
# =====================================================================

async def fetch_full_message(
    tenant_id: UUID, message_id: str,
    employee_id: UUID | None = None,
) -> dict | None:
    """Holt vollen Mail-Inhalt inkl. Body via Graph API.

    Returns: dict mit subject, from, body (text/html), receivedDateTime
    """
    try:
        access_token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    except Exception as e:
        logger.error(f"fetch_full_message Token-Fehler: {e}")
        return None

    # conversationId fuer Threading. internetMessageHeaders um In-Reply-To
    # als RFC-Fallback fuer find_open_conversation zu lesen — Graph
    # liefert die nur wenn explizit angefordert.
    params = {
        "$select": (
            "id,subject,from,toRecipients,body,bodyPreview,receivedDateTime,"
            "isRead,internetMessageId,conversationId,internetMessageHeaders,"
            "hasAttachments"
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{GRAPH_API_BASE}/me/messages/{message_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            if resp.status_code != 200:
                logger.error(
                    f"fetch_full_message fehlgeschlagen: {resp.status_code} {resp.text[:200]}"
                )
                return None
            return resp.json()
    except Exception as e:
        logger.exception(f"fetch_full_message Exception: {e}")
        return None


async def _forward_attachments_to_telegram(
    *, tenant_id: UUID, message_id: str, sender_label: str,
    subject: str, employee_id: UUID | None = None,
) -> int:
    """Lädt alle relevanten Anhaenge einer Mail und sendet sie als
    Telegram-Document/Photo an den passenden Mitarbeiter-Chat.

    Returns: Anzahl erfolgreich weitergeleiteter Anhaenge.
    """
    attachments = await fetch_attachments(
        tenant_id, message_id, employee_id=employee_id,
    )
    if not attachments:
        return 0

    # Telegram-Chat finden via _resolve_chat_id_for_push
    try:
        from plugins.telegram_notify.handler import (
            _resolve_chat_id_for_push,  # type: ignore
        )
        chat_id, bot_token = await _resolve_chat_id_for_push(
            tenant_id=tenant_id, employee_id=employee_id,
        )
    except Exception as e:
        logger.debug(f"Anhang-Forward: chat_id-Lookup failed: {e}")
        return 0

    if not chat_id or not bot_token:
        return 0

    # Pre-Header: was kommt
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        f"📎 <b>{len(attachments)} Anhang/Anhaenge</b> "
                        f"von {sender_label}\n"
                        f"Subject: {subject[:80]}"
                    ),
                    "parse_mode": "HTML",
                },
            )
    except Exception as e:
        # Preheader ist cosmetic — die eigentlichen Anhang-Uploads folgen
        # darunter und haben eigene Fehler-Logs. Debug-Level reicht.
        logger.debug(
            f"Anhang-Forward: Preheader-Send failed (Anhaenge gehen "
            f"trotzdem raus): {e}"
        )

    sent = 0
    for att in attachments:
        ct = (att.get("content_type") or "").lower()
        name = att.get("name") or "anhang"
        raw = att.get("bytes")
        if not raw:
            continue
        # Bilder via sendPhoto, alles andere via sendDocument
        is_image = ct.startswith("image/")
        endpoint = "sendPhoto" if is_image else "sendDocument"
        field_name = "photo" if is_image else "document"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/{endpoint}",
                    data={"chat_id": chat_id, "caption": name[:200]},
                    files={field_name: (name, raw, ct)},
                )
                if r.status_code == 200:
                    sent += 1
                else:
                    logger.warning(
                        f"Telegram-{endpoint} HTTP {r.status_code}: {r.text[:120]}"
                    )
        except Exception as e:
            logger.warning(f"Telegram-{endpoint} crashed: {e}")

    logger.info(
        f"Anhang-Forward: tenant={tenant_id} {sent}/{len(attachments)} "
        f"weitergeleitet"
    )
    return sent


async def fetch_attachments(
    tenant_id: UUID, message_id: str,
    employee_id: UUID | None = None,
    max_size_bytes: int = 10_000_000,
) -> list[dict]:
    """Holt alle FileAttachments einer Mail.

    Returns: [{'name': str, 'content_type': str, 'size': int, 'bytes': bytes}]

    Filter: ueberspringt Inline-Bilder (cid: Embedded), Anhaenge > 10MB
    und solche ohne contentBytes.
    """
    try:
        access_token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    except Exception as e:
        logger.error(f"fetch_attachments Token-Fehler: {e}")
        return []

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{GRAPH_API_BASE}/me/messages/{message_id}/attachments",
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "$select": "id,name,contentType,size,isInline,@odata.type,contentBytes",
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    f"fetch_attachments fehlgeschlagen: {resp.status_code} {resp.text[:200]}"
                )
                return []
            data = resp.json().get("value", [])
    except Exception as e:
        logger.exception(f"fetch_attachments Exception: {e}")
        return []

    import base64 as _b64
    out = []
    for att in data:
        # Nur fileAttachment, keine itemAttachment (eingebettete Mails)
        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue
        if att.get("isInline"):
            continue
        size = int(att.get("size") or 0)
        if size > max_size_bytes:
            logger.info(
                f"fetch_attachments skip {att.get('name')!r} - "
                f"{size} bytes > {max_size_bytes}"
            )
            continue
        content_b64 = att.get("contentBytes")
        if not content_b64:
            continue
        try:
            raw = _b64.b64decode(content_b64)
        except Exception as e:
            # Korruptes Attachment → skip, aber sichtbar im Log damit
            # systematische Decode-Probleme (z.B. Encoding-Drift bei
            # bestimmten Mail-Clients) auffallen.
            logger.warning(
                f"fetch_attachments: b64-decode failed fuer "
                f"{(att.get('name') or '?')!r} ({size} bytes): {e}"
            )
            continue
        out.append({
            "name": att.get("name") or "anhang",
            "content_type": att.get("contentType") or "application/octet-stream",
            "size": size,
            "bytes": raw,
        })
    return out


# =====================================================================
# Ordner-Management: "Gewerbeagent"-Ordner anlegen + Mails verschieben
# =====================================================================

GEWERBEAGENT_FOLDER_NAME = "Gewerbeagent"

# In-Memory-Cache: (tenant_id, employee_id) -> folder_id
# Phase 1 Multi-OAuth: jeder Mitarbeiter hat eigenes Postfach mit
# eigenem Gewerbeagent-Ordner — Cache-Key entsprechend trennen.
_folder_id_cache: dict[str, str] = {}


async def ensure_gewerbeagent_folder(
    tenant_id: UUID, employee_id: UUID | None = None,
) -> str | None:
    """Stellt sicher dass der 'Gewerbeagent'-Ordner existiert. Returnt Ordner-ID.

    Cached die ID in-memory pro Tenant+Mitarbeiter.
    """
    cache_key = f"{tenant_id}:{employee_id or 'default'}"
    if cache_key in _folder_id_cache:
        return _folder_id_cache[cache_key]

    try:
        access_token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    except Exception as e:
        logger.error(f"ensure_gewerbeagent_folder Token-Fehler: {e}")
        return None

    # 1) Existierende Top-Level-Ordner durchsuchen
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GRAPH_API_BASE}/me/mailFolders",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"$top": 100, "$select": "id,displayName"},
            )
            if resp.status_code != 200:
                logger.error(f"mailFolders-list fehlgeschlagen: {resp.status_code}")
                return None
            folders = resp.json().get("value", [])
            for f in folders:
                if f.get("displayName") == GEWERBEAGENT_FOLDER_NAME:
                    folder_id = f["id"]
                    _folder_id_cache[cache_key] = folder_id
                    logger.info(
                        f"Ordner '{GEWERBEAGENT_FOLDER_NAME}' existiert: tenant={tenant_id} "
                        f"folder_id={folder_id[:30]}..."
                    )
                    return folder_id
    except Exception as e:
        logger.exception(f"mailFolders-list Exception: {e}")
        return None

    # 2) Nicht gefunden - anlegen
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{GRAPH_API_BASE}/me/mailFolders",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"displayName": GEWERBEAGENT_FOLDER_NAME},
            )
            if resp.status_code not in (200, 201):
                logger.error(
                    f"mailFolders-create fehlgeschlagen: {resp.status_code} {resp.text[:200]}"
                )
                return None
            folder_id = resp.json().get("id")
            _folder_id_cache[cache_key] = folder_id
            logger.info(
                f"Ordner '{GEWERBEAGENT_FOLDER_NAME}' angelegt: tenant={tenant_id} "
                f"folder_id={folder_id[:30]}..."
            )
            return folder_id
    except Exception as e:
        logger.exception(f"mailFolders-create Exception: {e}")
        return None


async def move_to_gewerbeagent(
    tenant_id: UUID, message_id: str,
    employee_id: UUID | None = None,
) -> bool:
    """Verschiebt eine Mail in den Gewerbeagent-Ordner. Erstellt Ordner falls noetig."""
    folder_id = await ensure_gewerbeagent_folder(tenant_id, employee_id=employee_id)
    if not folder_id:
        logger.warning(f"move_to_gewerbeagent: kein folder_id, Mail bleibt in Inbox")
        return False

    try:
        access_token = await get_microsoft_token(tenant_id, employee_id=employee_id)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{GRAPH_API_BASE}/me/messages/{message_id}/move",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"destinationId": folder_id},
            )
            if resp.status_code in (200, 201):
                logger.info(
                    f"Mail verschoben nach Gewerbeagent: tenant={tenant_id} "
                    f"msg_id={message_id[:30]}..."
                )
                return True
            logger.error(
                f"messages-move fehlgeschlagen: {resp.status_code} {resp.text[:200]}"
            )
            return False
    except Exception as e:
        logger.exception(f"move_to_gewerbeagent Exception: {e}")
        return False


# =====================================================================
# Intent-Handler — Storno / Verschiebung / Rechnungsanfrage (Teil D.2)
# =====================================================================
# Werden vom poll_microsoft_inbox-Dispatch aufgerufen wenn intent !=
# neu_anfrage. Jeder Handler ist idempotent-light: bei Fehler in
# einzelnen Sub-Steps (z.B. mark_as_read) wird nur gewarnt — der
# Mail-Versand selbst ist das wichtige Outcome.

def _derive_kunde_anrede(sender_name: str, sender_email: str) -> str:
    """Vorname fuer "Hallo X,"-Anrede. Empty wenn nur Mail-Adresse
    erkennbar (kein echter Display-Name)."""
    if sender_name and "@" not in sender_name and sender_name != sender_email:
        return extract_first_name(sender_name)
    return ""


async def _mark_and_categorize_message(
    *, tenant_id: UUID, message_id: str | None,
    categories: list, target_category: str | None,
    employee_id: UUID | None, action_label: str, sender_email: str,
) -> None:
    """Kleiner Helper: Outlook-Kategorie setzen + mark_as_read.
    Schluckt Fehler mit Warning, weil der Hauptzweck (Mail bearbeitet)
    bereits erreicht wurde."""
    try:
        if target_category and message_id:
            await set_message_categories(
                tenant_id=tenant_id, message_id=message_id,
                categories=(categories or []) + [target_category],
                employee_id=employee_id,
            )
    except Exception as e:
        logger.warning(
            f"poll: Outlook-Kategorie auf {action_label} "
            f"sender={sender_email}: {e}"
        )
    try:
        if message_id:
            await mark_as_read(tenant_id, message_id, employee_id=employee_id)
    except Exception as e:
        logger.warning(
            f"poll: mark_as_read auf {action_label} sender={sender_email}: {e}"
        )


async def _handle_storno_intent(
    *,
    tenant: Tenant, tenant_id: UUID, message_id: str | None,
    sender_email: str, sender_name: str,
    subject: str, body_preview: str,
    existing_conv,  # EmailConversation | None
    employee_id: UUID | None,
    ms_conversation_id: str | None,
    classification: str, confidence: str, reason: str,
    categories: list,
) -> dict:
    """Stornier-Intent: Kalender-Cancel + Bestaetigungs-Mail + Push.

    Returns: process_result-Dict.
    """
    from core.integrations.mail_pipeline import (
        cancel_kunde_termine, send_storno_confirmation,
        push_tenant_intent_event, create_conversation, record_inbound,
        record_outbound_q_reply, set_conversation_state,
    )
    from core.models import STATE_STORNIERT

    # 1. Termine stornieren (auch wenn KEINE conv existiert — Kunde
    # koennte per Telefon gebucht haben und jetzt erstmals mailen)
    try:
        cancelled = await cancel_kunde_termine(
            tenant, sender_email, existing_conv,
        )
    except Exception as e:
        logger.exception(
            f"storno-intent: cancel_kunde_termine crash tenant={tenant.slug} "
            f"kunde={sender_email}: {e}"
        )
        cancelled = []

    # 2. Conversation anlegen falls neu
    conv = existing_conv
    if conv is None:
        try:
            conv = await create_conversation(
                tenant_id=tenant_id, sender_email=sender_email,
                sender_name=sender_name, subject=subject,
                microsoft_conversation_id=ms_conversation_id,
            )
        except Exception as e:
            logger.exception(
                f"storno-intent: create_conversation crash: {e}"
            )

    # 3. Record_inbound
    if conv is not None:
        try:
            await record_inbound(
                conv.id, last_user_message=body_preview,
                classification=classification,
                classification_confidence=confidence,
                classification_reason=reason,
                microsoft_conversation_id=ms_conversation_id,
            )
        except Exception as e:
            logger.warning(f"storno-intent: record_inbound: {e}")

    # 4. Storno-Bestaetigungs-Mail
    company_name = tenant.company_name or "Handwerksbetrieb"
    kunde_anrede = _derive_kunde_anrede(sender_name, sender_email)
    sent_meta: dict = {}
    try:
        sent_meta = await send_storno_confirmation(
            tenant_id=tenant_id, to_email=sender_email,
            kunde_anrede=kunde_anrede, company_name=company_name,
            original_subject=subject, cancelled_count=len(cancelled),
            employee_id=employee_id,
        )
    except Exception as e:
        logger.exception(f"storno-intent: send_storno_confirmation: {e}")

    # 5. Outbound-Mail persistieren (fuer Threading)
    if conv is not None and sent_meta.get("success"):
        try:
            reply_subject = (
                f"Re: {subject}" if not subject.lower().startswith("re:") else subject
            )
            await record_outbound_q_reply(
                conv.id,
                internet_message_id=sent_meta.get("internet_message_id"),
                microsoft_conversation_id=sent_meta.get("conversation_id"),
                q_reply_text=(
                    f"[Storno-Bestaetigung: {len(cancelled)} Termin(e) storniert]"
                ),
                subject=reply_subject,
            )
        except Exception as e:
            logger.warning(f"storno-intent: record_outbound_q_reply: {e}")
        # State auf STORNIERT — auch wenn cancelled=0 ist die Konversation
        # de-facto beendet (Kunde hat erklaert nicht zu wollen).
        try:
            await set_conversation_state(conv.id, STATE_STORNIERT)
        except Exception as e:
            logger.warning(f"storno-intent: set_conversation_state: {e}")

    # 6. Tenant-Push
    detail = (
        f"{len(cancelled)} Termin(e) storniert" if cancelled
        else "kein passender Termin gefunden — Rueckfrage-Mail versendet"
    )
    try:
        await push_tenant_intent_event(
            tenant=tenant, sender_email=sender_email, sender_name=sender_name,
            subject=subject, body_preview=body_preview,
            label="Storno verarbeitet", detail=detail, employee_id=employee_id,
        )
    except Exception as e:
        logger.warning(f"storno-intent: tenant-push: {e}")

    # 7. Outlook + read
    await _mark_and_categorize_message(
        tenant_id=tenant_id, message_id=message_id,
        categories=categories,
        target_category=Q_CATEGORY_BY_CLASSIFICATION.get("RELEVANT_KUNDE"),
        employee_id=employee_id, action_label="Storno-Mail",
        sender_email=sender_email,
    )

    return {
        "success": True, "skipped": False,
        "reason": "storno-processed",
        "intent": "termin_stornieren",
        "cancelled_count": len(cancelled),
        "conv_id": str(conv.id) if conv else None,
        "mail_sent": bool(sent_meta.get("success")),
    }


async def _handle_verschiebung_intent(
    *,
    tenant: Tenant, tenant_id: UUID, message_id: str | None,
    sender_email: str, sender_name: str,
    subject: str, body_preview: str,
    existing_conv, employee_id: UUID | None,
    ms_conversation_id: str | None,
    classification: str, confidence: str, reason: str,
    categories: list,
) -> dict:
    """Verschiebungs-Intent: Termin finden + Rueckfrage-Mail + Push.
    Stornieren tun wir NICHT — der Inhaber muss den neuen Termin
    erst buchen, dann den alten loeschen.
    """
    from core.integrations.mail_pipeline import (
        send_verschiebung_request, push_tenant_intent_event,
        create_conversation, record_inbound, record_outbound_q_reply,
    )
    from core.plugin_system import get_plugin_for_tenant

    # 1. Termine finden (read-only — kein cancel)
    found_termine: list[dict] = []
    try:
        kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
        if kalender is not None:
            find_res = await kalender.on_webhook(
                "find_events", {"kunde_email": sender_email.lower()},
            )
            if find_res.get("erfolg"):
                found_termine = list(find_res.get("termine", []))
    except Exception as e:
        logger.warning(
            f"verschiebung-intent: find_events tenant={tenant.slug} "
            f"kunde={sender_email}: {e}"
        )

    # 2. Conversation anlegen falls neu
    conv = existing_conv
    if conv is None:
        try:
            conv = await create_conversation(
                tenant_id=tenant_id, sender_email=sender_email,
                sender_name=sender_name, subject=subject,
                microsoft_conversation_id=ms_conversation_id,
            )
        except Exception as e:
            logger.exception(
                f"verschiebung-intent: create_conversation: {e}"
            )

    # 3. Record_inbound
    if conv is not None:
        try:
            await record_inbound(
                conv.id, last_user_message=body_preview,
                classification=classification,
                classification_confidence=confidence,
                classification_reason=reason,
                microsoft_conversation_id=ms_conversation_id,
            )
        except Exception as e:
            logger.warning(f"verschiebung-intent: record_inbound: {e}")

    # 4. Rueckfrage-Mail
    company_name = tenant.company_name or "Handwerksbetrieb"
    kunde_anrede = _derive_kunde_anrede(sender_name, sender_email)
    sent_meta: dict = {}
    try:
        sent_meta = await send_verschiebung_request(
            tenant_id=tenant_id, to_email=sender_email,
            kunde_anrede=kunde_anrede, company_name=company_name,
            original_subject=subject, found_termine=found_termine,
            employee_id=employee_id,
        )
    except Exception as e:
        logger.exception(f"verschiebung-intent: send mail: {e}")

    # 5. Outbound persistieren
    if conv is not None and sent_meta.get("success"):
        try:
            reply_subject = (
                f"Re: {subject}" if not subject.lower().startswith("re:") else subject
            )
            await record_outbound_q_reply(
                conv.id,
                internet_message_id=sent_meta.get("internet_message_id"),
                microsoft_conversation_id=sent_meta.get("conversation_id"),
                q_reply_text=(
                    f"[Verschiebungs-Rueckfrage: {len(found_termine)} "
                    f"Termin(e) gefunden, warte auf Wunschtermin]"
                ),
                subject=reply_subject,
            )
        except Exception as e:
            logger.warning(f"verschiebung-intent: record_outbound: {e}")

    # 6. Tenant-Push
    detail = (
        f"{len(found_termine)} Termin(e) gefunden, Rueckfrage raus"
        if found_termine
        else "kein Termin gefunden — Rueckfrage-Mail mit Bitte um Details"
    )
    try:
        await push_tenant_intent_event(
            tenant=tenant, sender_email=sender_email, sender_name=sender_name,
            subject=subject, body_preview=body_preview,
            label="Verschiebung erkannt", detail=detail,
            employee_id=employee_id,
        )
    except Exception as e:
        logger.warning(f"verschiebung-intent: tenant-push: {e}")

    # 7. Outlook + read
    await _mark_and_categorize_message(
        tenant_id=tenant_id, message_id=message_id,
        categories=categories,
        target_category=Q_CATEGORY_BY_CLASSIFICATION.get("RELEVANT_KUNDE"),
        employee_id=employee_id, action_label="Verschiebungs-Mail",
        sender_email=sender_email,
    )

    return {
        "success": True, "skipped": False,
        "reason": "verschiebung-processed",
        "intent": "termin_verschieben",
        "found_count": len(found_termine),
        "conv_id": str(conv.id) if conv else None,
        "mail_sent": bool(sent_meta.get("success")),
    }


async def _handle_rechnungsanfrage_intent(
    *,
    tenant: Tenant, tenant_id: UUID, message_id: str | None,
    sender_email: str, sender_name: str,
    subject: str, body_preview: str,
    existing_conv, employee_id: UUID | None,
    ms_conversation_id: str | None,
    classification: str, confidence: str, reason: str,
    categories: list,
) -> dict:
    """Rechnungsanfrage: nur Telegram-Push, keine Auto-Antwort.

    Rechnungen sind heikel (Mahnungen, Zahlungs-Disputes, Skonto,
    Steuer). Q soll hier NICHT halluzinieren. Inhaber kriegt den
    Push, antwortet manuell oder leitet an Lexware weiter.
    """
    from core.integrations.mail_pipeline import (
        push_tenant_intent_event, create_conversation, record_inbound,
    )

    # 1. Konversation tracken (auch ohne outbound — fuer Audit)
    conv = existing_conv
    if conv is None:
        try:
            conv = await create_conversation(
                tenant_id=tenant_id, sender_email=sender_email,
                sender_name=sender_name, subject=subject,
                microsoft_conversation_id=ms_conversation_id,
            )
        except Exception as e:
            logger.exception(
                f"rechnung-intent: create_conversation: {e}"
            )
    if conv is not None:
        try:
            await record_inbound(
                conv.id, last_user_message=body_preview,
                classification=classification,
                classification_confidence=confidence,
                classification_reason=reason,
                microsoft_conversation_id=ms_conversation_id,
            )
        except Exception as e:
            logger.warning(f"rechnung-intent: record_inbound: {e}")

    # 2. Push
    try:
        await push_tenant_intent_event(
            tenant=tenant, sender_email=sender_email, sender_name=sender_name,
            subject=subject, body_preview=body_preview,
            label="Rechnungsanfrage", detail="keine Auto-Antwort — manuell pruefen",
            employee_id=employee_id,
        )
    except Exception as e:
        logger.warning(f"rechnung-intent: tenant-push: {e}")

    # 3. Outlook + read — als RELEVANT_GESCHAEFT (nicht KUNDE) damit der
    # Inhaber in Outlook trennen kann zwischen Auftrags- und Rechnungs-
    # Korrespondenz.
    await _mark_and_categorize_message(
        tenant_id=tenant_id, message_id=message_id,
        categories=categories,
        target_category=Q_CATEGORY_BY_CLASSIFICATION.get("RELEVANT_GESCHAEFT"),
        employee_id=employee_id, action_label="Rechnungsanfrage",
        sender_email=sender_email,
    )

    return {
        "success": True, "skipped": False,
        "reason": "rechnung-pushed",
        "intent": "rechnungsanfrage",
        "conv_id": str(conv.id) if conv else None,
    }


# =====================================================================
# Pipeline: RELEVANT_KUNDE Mail komplett verarbeiten
# (Body holen + Token erstellen + Antwort senden + verschieben)
# =====================================================================

async def process_relevant_kunde_mail(
    tenant_id: UUID,
    message_id: str,
    classification_result: dict,
    employee_id: UUID | None = None,
) -> dict:
    """Verarbeitet eine als RELEVANT_KUNDE klassifizierte Mail komplett.

    Schritte:
    1. Vollen Body holen
    2. Anfrage-Token + URL erstellen
    3. KI-Antwort generieren mit Wissensbasis-Kontext + Formular-Link
    4. Antwort via Microsoft Graph aus Tenant-Adresse senden
    5. Original-Mail in 'Gewerbeagent'-Ordner verschieben

    Returns: {success, sent, moved, token, error?}
    """
    from core.ai.gemini import generate_anfrage_reply
    from core.integrations.anfrage_forms import (
        create_anfrage_token,
        build_anfrage_url,
    )
    from core.integrations.microsoft import send_mail_as_user
    from core.models import ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN, Tenant
    from core.database import AsyncSessionLocal
    from sqlalchemy import select as _sel

    result = {"success": False, "sent": False, "moved": False, "token": None}

    # 1) Vollen Body holen
    full = await fetch_full_message(tenant_id, message_id, employee_id=employee_id)
    if not full:
        result["error"] = "fetch_full_message fehlgeschlagen"
        return result

    subject = full.get("subject", "(kein Betreff)") or "(kein Betreff)"
    from_obj = (full.get("from") or {}).get("emailAddress") or {}
    sender_email = from_obj.get("address", "") or "unbekannt"
    sender_name = from_obj.get("name", "") or sender_email
    body_obj = full.get("body") or {}
    body_text = body_obj.get("content", "") or full.get("bodyPreview", "") or ""
    # Wenn HTML, simpel strippen
    if body_obj.get("contentType", "").lower() == "html":
        import re as _re
        body_text = _re.sub(r"<[^>]+>", " ", body_text)
        body_text = _re.sub(r"\s+", " ", body_text).strip()
    internet_message_id = full.get("internetMessageId", "")

    # 2) Tenant + Wissensbasis laden
    async with AsyncSessionLocal() as session:
        t_res = await session.execute(_sel(Tenant).where(Tenant.id == tenant_id))
        tenant = t_res.scalar_one_or_none()
    if not tenant:
        result["error"] = "Tenant nicht gefunden"
        return result

    tenant_company = tenant.company_name or "Handwerksbetrieb"
    tenant_branche = getattr(tenant, "branche", None) or "Handwerk"
    # Inhaber-Vorname aus tenant.contact_name extrahieren statt company_name
    # zu splitten — "Tischlerei Dietz".split()[0] gibt "Tischlerei", was als
    # Personen-Name in Anrede/Signatur peinlich ist. Bei leerem Vorname
    # signiert generate_anfrage_reply mit "Ihr Team von {tenant_company}".
    tenant_owner_first = extract_first_name(tenant.contact_name or "") or None

    # Wissensbasis als Text laden (best-effort)
    wissensbasis_text = "(noch keine spezifischen Infos hinterlegt)"
    try:
        from core.models import TenantKnowledge
        async with AsyncSessionLocal() as session:
            k_res = await session.execute(
                _sel(TenantKnowledge).where(TenantKnowledge.tenant_id == tenant_id)
            )
            entries = k_res.scalars().all()
            if entries:
                lines = []
                for e in entries[:20]:
                    cat = getattr(e, "kategorie", "") or ""
                    txt = getattr(e, "inhalt", "") or ""
                    if txt:
                        lines.append(f"- [{cat}] {txt[:300]}")
                if lines:
                    wissensbasis_text = "\n".join(lines)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Wissensbasis laden fehler: {e}")

    # 3) Token + URL erstellen
    anfrage_typ = ANFRAGE_TYP_TISCHLER if "tischler" in tenant_branche.lower() else ANFRAGE_TYP_ALLGEMEIN
    try:
        token_obj = await create_anfrage_token(
            tenant_id=tenant_id,
            kunde_email=sender_email,
            kunde_name=sender_name,
            anfrage_typ=anfrage_typ,
            original_subject=subject,
            original_message_id=internet_message_id,
            valid_days=14,
        )
        form_url = build_anfrage_url(token_obj.token)
        result["token"] = token_obj.token
    except Exception as e:
        logger.exception(f"Token-Erstellung fehler: {e}")
        result["error"] = f"Token-Erstellung: {e}"
        return result

    # 4) KI-Antwort generieren
    try:
        reply_text = await generate_anfrage_reply(
            subject=subject,
            sender_name=sender_name,
            sender_email=sender_email,
            body=body_text,
            form_url=form_url,
            tenant_company=tenant_company,
            tenant_branche=tenant_branche,
            tenant_owner_first_name=tenant_owner_first,
            wissensbasis=wissensbasis_text,
        )
    except Exception as e:
        logger.exception(f"KI-Reply fehler: {e}")
        result["error"] = f"KI-Reply: {e}"
        return result

    # 5) Mail-HTML mit professionellem Template bauen
    # Vorname extrahieren - NUR aus echtem Display-Name, nicht aus E-Mail
    if sender_name and "@" not in sender_name and sender_name != sender_email:
        kunde_anrede = extract_first_name(sender_name)
    else:
        kunde_anrede = ""

    body_html = build_kunde_reply_html(
        kunde_anrede_name=kunde_anrede,
        kunde_email=sender_email,
        reply_text=reply_text,
        form_url=form_url,
        company_name=tenant_company,
        contact_name=getattr(tenant, "contact_name", "") or "",
        contact_email=getattr(tenant, "contact_email", "") or "",
        contact_phone=getattr(tenant, "contact_phone", "") or "",
    )

    # Mail via Microsoft Graph senden (aus dem Postfach des Mitarbeiters).
    # send_tracked_mail (Draft-Create + Send) statt send_mail_as_user
    # weil wir die internetMessageId + conversationId brauchen um die
    # ausgehende Q-Antwort in der EmailConversation zu persistieren
    # (Reply-Threading: naechste Kunden-Reply hat In-Reply-To =
    # diese internetMessageId).
    reply_subject = (
        f"Re: {subject}" if not subject.lower().startswith("re:") else subject
    )
    sent_meta: dict = {}
    try:
        from core.integrations.microsoft import send_tracked_mail
        sent_meta = await send_tracked_mail(
            tenant_id=tenant_id,
            to_email=sender_email,
            subject=reply_subject,
            body_html=body_html,
            employee_id=employee_id,
        )
        result["sent"] = bool(sent_meta.get("success"))
        if not sent_meta.get("success"):
            err = sent_meta.get("error") or "send_tracked_mail returnte success=False"
            result["error"] = f"Mail-Versand: {err}"
            logger.warning(
                f"send_tracked_mail fehlgeschlagen: tenant={tenant_id} "
                f"to={sender_email}: {err}"
            )
    except Exception as e:
        logger.exception(f"Mail-Versand fehler: {e}")
        result["error"] = f"Mail-Versand: {e}"
        return result

    # Threading-Persistenz: neue EmailConversation anlegen + ausgehende
    # Q-Mail-IDs persistieren. Wir kommen hier nur an wenn KEIN
    # bestehender Thread gefunden wurde (poll-Loop hat das geprueft) —
    # also klassischer Neukunde-Fall.
    if result["sent"]:
        try:
            from core.integrations.mail_pipeline import (
                create_conversation, record_outbound_q_reply, record_inbound,
            )
            ms_conv_id = sent_meta.get("conversation_id")
            outbound_imsg_id = sent_meta.get("internet_message_id")
            new_conv = await create_conversation(
                tenant_id=tenant_id,
                sender_email=sender_email,
                sender_name=sender_name,
                subject=subject,
                microsoft_conversation_id=ms_conv_id,
            )
            await record_inbound(
                new_conv.id,
                last_user_message=body_text[:4000] if body_text else None,
                classification=(classification_result or {}).get(
                    "classification"
                ),
                classification_confidence=(classification_result or {}).get(
                    "confidence"
                ),
                classification_reason=(classification_result or {}).get(
                    "reason"
                ),
            )
            await record_outbound_q_reply(
                new_conv.id,
                internet_message_id=outbound_imsg_id,
                microsoft_conversation_id=ms_conv_id,
                q_reply_text=reply_text,
                subject=reply_subject,
            )
            result["conv_id"] = str(new_conv.id)
        except Exception as e:
            # Persistierung darf den Mail-Versand-Erfolg nicht killen
            # — Mail ist raus, Token existiert, Anhang-Forward folgt.
            # Threading geht beim naechsten Reply halt nicht, das ist
            # nicht schoen aber kein Datenverlust.
            logger.exception(
                f"Threading-Persistenz fehler (Mail wurde gesendet, "
                f"conv nicht angelegt): {e}"
            )

    # 6) Original-Mail aufraeumen (nur wenn Send erfolgreich)
    #    a) isRead=true setzen - Defense-in-Depth: selbst wenn der
    #       Inbox-Filter mal greift bevor die Mail verschoben ist,
    #       wird sie durch "isRead eq false" nicht mehr gefangen.
    #    b) Dann in Gewerbeagent-Ordner verschieben.
    if result["sent"]:
        try:
            read_ok = await mark_as_read(tenant_id, message_id, employee_id=employee_id)
            if not read_ok:
                logger.warning(
                    f"mark_as_read returnte False: tenant={tenant_id} "
                    f"msg={message_id[:30]}..."
                )
        except Exception as e:
            logger.warning(f"mark_as_read fehler (non-fatal): {e}")

        # 6c) Anhaenge an Telegram weiterleiten (Bilder, PDFs).
        # Best-effort, schluckt eigene Fehler. Inhaber sieht so direkt
        # das Foto vom kaputten Heizkessel oder den PDF-Plan.
        if full.get("hasAttachments"):
            try:
                await _forward_attachments_to_telegram(
                    tenant_id=tenant_id,
                    message_id=message_id,
                    sender_label=f"{sender_name} ({sender_email})",
                    subject=subject,
                    employee_id=employee_id,
                )
            except Exception as e:
                logger.warning(f"Anhang-Forward fehler (non-fatal): {e}")

        try:
            moved = await move_to_gewerbeagent(
                tenant_id, message_id, employee_id=employee_id,
            )
            result["moved"] = moved
        except Exception as e:
            logger.warning(f"Mail-Move fehler (non-fatal): {e}")

    result["success"] = result["sent"]
    logger.info(
        f"process_relevant_kunde_mail: tenant={tenant_id} from={sender_email} "
        f"sent={result['sent']} moved={result['moved']} token={result.get('token','')[:10]}..."
    )
    return result


# =====================================================================
# Outlook-Kategorien fuer Q-Klassifikation
# Daniel sieht in Outlook auf einen Blick was Q schon angeschaut hat.
# =====================================================================

# Mapping Klassifikation -> Outlook-Kategorie-Name
Q_CATEGORY_BY_CLASSIFICATION = {
    "NICHT_RELEVANT": "Q-Werbung",
    "PRIVAT": "Q-Privat",
    "RELEVANT_GESCHAEFT": "Q-Geschaeft",
    "UNSICHER": "Q-Unsicher",
    # RELEVANT_KUNDE bekommt KEINE Kategorie - wird ja in Ordner verschoben
}

# Alle Q-Kategorien (fuer Filter "nicht Q-markiert")
ALL_Q_CATEGORIES = list(Q_CATEGORY_BY_CLASSIFICATION.values())


async def set_message_categories(
    tenant_id: UUID, message_id: str, categories: list[str],
    employee_id: UUID | None = None,
) -> bool:
    """Setzt Outlook-Kategorien auf einer Mail via Graph API.

    Categories werden in Outlook als farbige Labels angezeigt.
    Microsoft erstellt die Kategorie automatisch falls nicht vorhanden.
    Returns True bei Erfolg.
    """
    try:
        access_token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    except Exception as e:
        logger.error(f"set_message_categories Token-Fehler: {e}")
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(
                f"{GRAPH_API_BASE}/me/messages/{message_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"categories": categories},
            )
            if resp.status_code in (200, 204):
                logger.info(
                    f"Kategorie gesetzt: tenant={tenant_id} "
                    f"msg={message_id[:30]}... cats={categories}"
                )
                return True
            logger.error(
                f"set_message_categories fehlgeschlagen: "
                f"{resp.status_code} {resp.text[:200]}"
            )
            return False
    except Exception as e:
        logger.exception(f"set_message_categories Exception: {e}")
        return False

