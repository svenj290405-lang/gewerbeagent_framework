"""Microsoft Graph API Integration: Mail senden im Namen des Tenants.

Nutzt OAuth-Tokens aus oauth_tokens-Tabelle, refreshed automatisch wenn abgelaufen.

Hauptfunktionen:
- get_microsoft_token(tenant_id) -> str: liefert gueltigen access_token
- send_mail_as_user(tenant_id, ...): schickt Mail via Graph API
- get_microsoft_status(tenant_id): zeigt Verbindungsstatus
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import OAuthToken
from core.security.oauth_flow import (
    MICROSOFT_TOKEN_URL,
    _load_microsoft_config,
)

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


class MicrosoftNotConnectedError(Exception):
    """Tenant hat Microsoft-Account nicht verbunden."""
    pass


async def _refresh_access_token(oauth_token: OAuthToken) -> tuple[str, datetime]:
    """Holt neuen access_token via refresh_token.

    Race-Schutz: SELECT FOR UPDATE auf der Token-Zeile serialisiert
    parallele Refreshes. Wenn Request A bereits refreshed hat und der
    Token in der DB schon frisch ist, gibt B den frischen Token direkt
    zurueck statt einen zweiten Refresh-Call zu machen.

    Returns: (new_access_token, expires_at)
    """
    # 1) Pessimistic Lock auf die Token-Zeile + Re-Check: vielleicht hat
    # ein paralleler Request bereits refreshed.
    async with AsyncSessionLocal() as lock_session:
        result = await lock_session.execute(
            select(OAuthToken)
            .where(OAuthToken.id == oauth_token.id)
            .with_for_update()
        )
        locked_token = result.scalar_one()
        now = datetime.now(timezone.utc)
        expires = locked_token.access_token_expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)

        if locked_token.access_token and expires and expires > now:
            # Anderer Request war schneller — wir nutzen seinen Token.
            await lock_session.commit()
            return locked_token.access_token, expires

        # Wir muessen tatsaechlich refreshen. Lock bleibt bis Commit.
        cfg = await _load_microsoft_config()

        # Beim Refresh KEIN `scope`-Param senden — per OAuth-RFC 6749 §6
        # nutzt Microsoft dann automatisch die urspruenglich gegranteten
        # Scopes. Wuerden wir die globale MICROSOFT_SCOPES-Liste mitsenden
        # und dort waere ein Scope dazugekommen, der beim urspruenglichen
        # Consent nicht dabei war, gibt es AADSTS70000 invalid_grant
        # (Incident 2026-05-17 nach MailboxSettings.Read-Erweiterung).
        # Bestehende Tokens refreshen jetzt sauber; neue OAuth-Flows
        # kriegen die volle Scope-Liste via /authorize.
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    MICROSOFT_TOKEN_URL,
                    data={
                        "client_id": cfg["client_id"],
                        "client_secret": cfg["client_secret"],
                        "refresh_token": locked_token.refresh_token,
                        "grant_type": "refresh_token",
                    },
                    headers={"Accept": "application/json"},
                )
        except Exception as exc:
            await lock_session.rollback()
            raise ValueError(f"Microsoft Token-Refresh Connection-Fehler: {exc}") from exc

        if resp.status_code != 200:
            # 401/400 = refresh_token ungueltig (User hat Zugriff entzogen).
            # Wir benachrichtigen den Tenant ueber Telegram damit er
            # /kalender_verbinden erneut macht. Failsafe — Telegram-
            # Fehler bricht den Refresh-Pfad nicht.
            if resp.status_code in (400, 401):
                try:
                    from core.integrations.tenant_alert import (
                        notify_oauth_revoked,
                    )
                    await notify_oauth_revoked(
                        tenant_id=locked_token.tenant_id,
                        provider="microsoft",
                        employee_id=locked_token.employee_id,
                    )
                except Exception as alert_exc:
                    logger.debug(f"OAuth-Alert failed (egal): {alert_exc}")
            await lock_session.rollback()
            raise ValueError(
                f"Microsoft Token-Refresh fehlgeschlagen: "
                f"{resp.status_code} {resp.text[:300]}"
            )

        tokens = resp.json()
        new_access = tokens.get("access_token")
        new_refresh = tokens.get("refresh_token")
        expires_in = int(tokens.get("expires_in", 3600))

        if not new_access:
            await lock_session.rollback()
            raise ValueError("Microsoft hat keinen access_token zurueckgegeben")

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

        # Update unter dem Lock — Commit gibt den Lock frei.
        locked_token.access_token = new_access
        locked_token.access_token_expires_at = expires_at
        if new_refresh:
            locked_token.refresh_token = new_refresh
        await lock_session.commit()

    logger.info(
        f"Microsoft access_token erneuert fuer tenant_id={oauth_token.tenant_id}"
    )
    return new_access, expires_at


async def get_microsoft_token(
    tenant_id: UUID, employee_id: UUID | None = None,
) -> str:
    """Liefert gueltigen access_token fuer einen Tenant/Mitarbeiter.

    Phase 1 Multi-OAuth: optional employee_id — nutzt zentralen Lookup
    mit 3-stufigem Fallback (employee → default-emp → legacy-tenant).
    Backward-Compat: ohne employee_id verhaelt sich die Funktion exakt
    wie vorher (Tenant-weiter Lookup via Default-Employee-Backfill).

    Refreshed access_token automatisch wenn abgelaufen.

    Raises MicrosoftNotConnectedError wenn kein Token zu finden ist.
    """
    from core.security.oauth_token_lookup import find_oauth_token

    oauth_token = await find_oauth_token(tenant_id, "microsoft", employee_id)
    if not oauth_token:
        scope = f"emp={employee_id}" if employee_id else f"tenant={tenant_id}"
        raise MicrosoftNotConnectedError(
            f"Microsoft-Account nicht verbunden ({scope}). "
            "Mit /kalender_verbinden im Telegram einrichten."
        )

    now = datetime.now(timezone.utc)
    expires = oauth_token.access_token_expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    if oauth_token.access_token and expires and expires > now:
        return oauth_token.access_token

    new_token, _ = await _refresh_access_token(oauth_token)
    return new_token


def _build_attachment_payload(attachments: Optional[list[dict]]) -> list[dict]:
    """Konvertiert [{filename, bytes, content_type}] in Microsoft-Graph-fileAttachment-Format."""
    if not attachments:
        return []
    import base64 as _b64
    out = []
    for a in attachments:
        raw = a.get("bytes")
        if raw is None:
            continue
        out.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": a.get("filename") or "anhang.pdf",
            "contentType": a.get("content_type") or "application/pdf",
            "contentBytes": _b64.b64encode(raw).decode("ascii"),
        })
    return out


async def send_mail_as_user(
    tenant_id: UUID,
    to_email: str,
    subject: str,
    body_html: str,
    cc: Optional[list[str]] = None,
    save_to_sent: bool = True,
    attachments: Optional[list[dict]] = None,
    employee_id: UUID | None = None,
) -> bool:
    """Sendet Mail im Namen des verbundenen Microsoft-Users via Graph API.

    Phase 1 Multi-OAuth: optional employee_id — sendet aus dem Postfach
    eines bestimmten Mitarbeiters (statt nur Tenant-Default).

    attachments: [{"filename": "...", "bytes": b"...", "content_type": "application/pdf"}]
    Returns: True bei Erfolg, False bei Fehler.
    """
    try:
        access_token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    except MicrosoftNotConnectedError:
        logger.warning(f"send_mail_as_user: Tenant {tenant_id} nicht mit Microsoft verbunden")
        return False
    except Exception as e:
        logger.error(f"send_mail_as_user Token-Fehler: {e}")
        return False

    message_obj = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
    }
    if cc:
        message_obj["ccRecipients"] = [
            {"emailAddress": {"address": addr}} for addr in cc
        ]
    att_payload = _build_attachment_payload(attachments)
    if att_payload:
        message_obj["attachments"] = att_payload

    message = {"message": message_obj, "saveToSentItems": save_to_sent}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{GRAPH_API_BASE}/me/sendMail",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=message,
            )
            if resp.status_code in (200, 202):
                logger.info(
                    f"Microsoft-Mail gesendet: tenant={tenant_id} to={to_email} "
                    f"subject={subject[:50]!r} attachments={len(att_payload)}"
                )
                # Failsafe Usage-Tracking
                try:
                    from core.billing import track_mail_send
                    await track_mail_send(
                        "microsoft",
                        tenant_id=tenant_id,
                        operation="mail-send",
                        recipient_count=1 + (len(cc) if cc else 0),
                        recipient_email=to_email,
                    )
                except Exception as e:
                    logger.debug(f"Microsoft-Tracking failed (egal): {e}")
                return True
            logger.error(
                f"Microsoft sendMail fehlgeschlagen: "
                f"{resp.status_code} {resp.text[:300]}"
            )
            return False
    except Exception as e:
        logger.exception(f"send_mail_as_user Exception: {e}")
        return False


async def send_tracked_mail(
    tenant_id: UUID,
    to_email: str,
    subject: str,
    body_html: str,
    cc: Optional[list[str]] = None,
    attachments: Optional[list[dict]] = None,
    employee_id: UUID | None = None,
) -> dict:
    """Versendet eine Mail im Two-Step-Modus (Draft-Create + Send) damit wir
    die Microsoft-IDs bekommen, ueber die wir spaeter Antworten zuordnen koennen.

    Schritte:
      1. POST /me/messages           -> Draft anlegen, kriegt id, internetMessageId, conversationId
      2. (optional) PATCH falls cc/etc nachgepflegt werden muessten - hier Step 1 deckt alles
      3. POST /me/messages/{id}/send -> Mail rausgeben (Body: leer, das Draft wird genommen)

    Returns: {
        success: bool,
        message_id: str | None,
        internet_message_id: str | None,
        conversation_id: str | None,
        error: str | None,
    }
    """
    out = {
        "success": False,
        "message_id": None,
        "internet_message_id": None,
        "conversation_id": None,
        "error": None,
    }
    try:
        access_token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    except MicrosoftNotConnectedError:
        out["error"] = "Microsoft nicht verbunden"
        return out
    except Exception as e:
        out["error"] = f"Token-Fehler: {e}"
        return out

    draft_payload = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
    }
    if cc:
        draft_payload["ccRecipients"] = [
            {"emailAddress": {"address": addr}} for addr in cc
        ]

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1) Draft anlegen
            r1 = await client.post(
                f"{GRAPH_API_BASE}/me/messages",
                headers=headers,
                json=draft_payload,
            )
            if r1.status_code not in (200, 201):
                out["error"] = f"Draft-Create {r1.status_code}: {r1.text[:300]}"
                return out
            draft = r1.json()
            msg_id = draft.get("id")
            out["message_id"] = msg_id
            out["internet_message_id"] = draft.get("internetMessageId")
            out["conversation_id"] = draft.get("conversationId")

            # 2) Anhaenge dranbauen (einzeln POSTen, weil das Draft schon existiert)
            for a in _build_attachment_payload(attachments):
                ra = await client.post(
                    f"{GRAPH_API_BASE}/me/messages/{msg_id}/attachments",
                    headers=headers,
                    json=a,
                )
                if ra.status_code not in (200, 201):
                    out["error"] = (
                        f"Attachment '{a.get('name')}' fehlgeschlagen: "
                        f"{ra.status_code} {ra.text[:200]}"
                    )
                    # Draft aufraeumen
                    await client.delete(
                        f"{GRAPH_API_BASE}/me/messages/{msg_id}", headers=headers
                    )
                    return out

            # 3) Senden
            rs = await client.post(
                f"{GRAPH_API_BASE}/me/messages/{msg_id}/send",
                headers=headers,
            )
            if rs.status_code not in (200, 202):
                out["error"] = f"Send {rs.status_code}: {rs.text[:300]}"
                return out

        out["success"] = True
        logger.info(
            f"send_tracked_mail OK: tenant={tenant_id} to={to_email} "
            f"msg_id={(out['message_id'] or '')[:30]} "
            f"conv_id={(out['conversation_id'] or '')[:30]}"
        )
    except Exception as e:
        logger.exception(f"send_tracked_mail Exception: {e}")
        out["error"] = str(e)

    return out


async def get_microsoft_status(
    tenant_id: UUID, employee_id: UUID | None = None,
) -> dict:
    """Zeigt Verbindungs-Status fuer Tenant/Mitarbeiter.

    Phase 1 Multi-OAuth: optional employee_id — nutzt zentralen Lookup
    (employee → default-employee → legacy-tenant).

    Returns: {connected, account_email, expires_at, scopes}
    """
    from core.security.oauth_token_lookup import find_oauth_token

    oauth_token = await find_oauth_token(tenant_id, "microsoft", employee_id)

    if not oauth_token:
        return {
            "connected": False,
            "account_email": None,
            "expires_at": None,
            "scopes": None,
        }

    return {
        "connected": True,
        "account_email": oauth_token.account_email,
        "expires_at": oauth_token.access_token_expires_at,
        "scopes": oauth_token.scopes,
    }
