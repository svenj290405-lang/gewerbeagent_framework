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

    params = {
        "$filter": full_filter,
        "$select": "id,subject,from,bodyPreview,categories,receivedDateTime,isRead",
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
            except Exception:
                pass
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

        # Klassifikation - Subject + Sender + Preview als Hilfe
        # Hinweis: Wir geben Preview auch mit damit Gemini bessere Entscheidung trifft
        try:
            cls_result = await classify_mail_subject(
                subject=f"{subject} [Preview: {body_preview[:200]}]",
                sender=sender_email,
                tenant_company=tenant_company,
                tenant_branche=tenant_branche,
            )
            classification = cls_result.get("classification") or "UNSICHER"
            confidence = cls_result.get("confidence") or "low"
            reason = cls_result.get("reason") or ""
        except Exception as e:
            logger.warning(f"Klassifikation fehler fuer msg {msg.get('id')}: {e}")
            classification = "UNSICHER"
            confidence = "low"
            reason = f"Fehler: {e}"

        classified_counts[classification] = classified_counts.get(classification, 0) + 1

        # Auto-Verarbeitung NUR bei RELEVANT_KUNDE und nicht throttled.
        # Confidence-Gate: bei "low" eskalieren statt blind auto-antworten,
        # damit Q nicht auf falsch verstandene Mails halluziniert.
        process_result = None
        if classification == "RELEVANT_KUNDE":
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
                except Exception:
                    pass
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
                except Exception:
                    pass
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

    params = {
        "$select": "id,subject,from,toRecipients,body,bodyPreview,receivedDateTime,isRead,internetMessageId",
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
    tenant_owner = (tenant.company_name or "der Betrieb").split()[0]

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
            tenant_owner_first_name=tenant_owner,
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
        contact_name=getattr(tenant, "contact_name", "") or tenant_owner,
        contact_email=getattr(tenant, "contact_email", "") or "",
        contact_phone=getattr(tenant, "contact_phone", "") or "",
    )

    # Mail senden via Microsoft Graph (aus dem Postfach des Mitarbeiters)
    try:
        sent_ok = await send_mail_as_user(
            tenant_id=tenant_id,
            to_email=sender_email,
            subject=f"Re: {subject}" if not subject.lower().startswith("re:") else subject,
            body_html=body_html,
            save_to_sent=True,
            employee_id=employee_id,
        )
        result["sent"] = bool(sent_ok)
        if not sent_ok:
            result["error"] = "Mail-Versand fehlgeschlagen"
            logger.warning(f"send_mail_as_user returnte False: tenant={tenant_id} to={sender_email}")
    except Exception as e:
        logger.exception(f"Mail-Versand fehler: {e}")
        result["error"] = f"Mail-Versand: {e}"
        return result

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

