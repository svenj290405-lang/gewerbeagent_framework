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
from core.integrations.microsoft import (
    GRAPH_API_BASE,
    MicrosoftNotConnectedError,
    get_microsoft_token,
)
from core.models import EmailConversation, Tenant

logger = logging.getLogger(__name__)


async def fetch_unread_messages(
    tenant_id: UUID, top: int = 25
) -> list[dict]:
    """Holt die letzten N ungelesenen Mails (Header + Preview, nicht voller Body).

    Returns: Liste von Mail-Dicts mit id, subject, from, bodyPreview, receivedDateTime, isRead
    """
    access_token = await get_microsoft_token(tenant_id)

    # Nur Felder holen die wir brauchen - bodyPreview ist max 255 Zeichen
    params = {
        "$filter": "isRead eq false",
        "$select": "id,subject,from,bodyPreview,receivedDateTime,isRead",
        "$orderby": "receivedDateTime desc",
        "$top": top,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{GRAPH_API_BASE}/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        if resp.status_code != 200:
            raise ValueError(
                f"Graph /me/messages fehlgeschlagen: {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json()
        return data.get("value", [])


async def mark_as_read(tenant_id: UUID, message_id: str) -> bool:
    """Markiert eine Mail als gelesen via Graph API."""
    try:
        access_token = await get_microsoft_token(tenant_id)
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


async def poll_microsoft_inbox(tenant_id: UUID) -> dict:
    """Hauptfunktion: Hol ungelesene Mails fuer Tenant, klassifiziere alle.

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
        messages = await fetch_unread_messages(tenant_id, top=25)
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
