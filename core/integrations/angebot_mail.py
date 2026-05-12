"""Versand-Pipeline fuer Lexware-Angebote per Microsoft Graph mit PDF-Anhang.

Bündelt:
1. Lexware-Status pruefen + ggf. Hinweis dass Finalisierung noetig ist
2. PDF runterladen
3. Mail-Body bauen
4. Two-Step-Send via send_tracked_mail (damit wir conversation_id + internetMessageId
   spaeter zur Antwort-Zuordnung nutzen koennen)
5. Tracking-IDs am Angebot persistieren

Wird genutzt vom Telegram-Wizard /angebot und vom microsoft_inbox-Polling
(Auto-Rechnung-Pfad nutzt parallel send_tracked_mail mit Rechnungs-PDF).
"""
from __future__ import annotations

import datetime as dt
import html as _html
import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.integrations.accounting_base import AccountingError
from core.integrations.lexware import LexwareProvider
from core.integrations.microsoft import send_tracked_mail
from core.models import Angebot, Tenant, ToolConfig
from core.security import decrypt

logger = logging.getLogger(__name__)


def _format_eur(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(value)


def _build_angebot_mail_html(
    *,
    kunde_anrede: str,
    angebot_nummer: str,
    gesamtbetrag_brutto_eur,
    company_name: str,
    contact_name: str,
    contact_email: str,
    contact_phone: str,
) -> str:
    """Apple-clean HTML-Body fuer das Angebots-Anschreiben mit PDF-Anhang.

    Bewusst minimalistisch - das Angebot selbst ist die PDF.
    """
    anrede_part = f"Hallo {_html.escape(kunde_anrede)}," if kunde_anrede else "Hallo,"
    summe = _format_eur(gesamtbetrag_brutto_eur) if gesamtbetrag_brutto_eur else ""
    summe_block = (
        f'<p style="margin: 0 0 16px;">Gesamtsumme: <b>{summe}</b> brutto.</p>'
        if summe else ""
    )

    contact_lines = []
    if contact_name:
        contact_lines.append(_html.escape(contact_name))
    if contact_email:
        contact_lines.append(
            f'<a href="mailto:{_html.escape(contact_email)}" style="color: #1d1d1f;">'
            f'{_html.escape(contact_email)}</a>'
        )
    if contact_phone:
        contact_lines.append(_html.escape(contact_phone))
    contact_html = "<br>".join(contact_lines)

    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f5f5f7;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',sans-serif;color:#1d1d1f;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f7;padding:32px 0;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:14px;padding:36px 40px;box-shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.04);">
<tr><td style="font-size:17px;line-height:1.5;">
<p style="margin:0 0 16px;">{anrede_part}</p>
<p style="margin:0 0 16px;">vielen Dank fuer Ihre Anfrage. Anbei finden Sie unser Angebot
{f"<b>{_html.escape(angebot_nummer)}</b>" if angebot_nummer else ""} als PDF im Anhang.</p>
{summe_block}
<p style="margin:0 0 16px;">Bei Rueckfragen oder fuer eine Beauftragung antworten Sie einfach auf diese Mail.</p>
<p style="margin:24px 0 0;">Viele Gruesse<br>
{contact_html or _html.escape(company_name)}</p>
</td></tr>
</table>
<p style="font-size:12px;color:#86868b;margin:20px 0 0;text-align:center;">
{_html.escape(company_name)}
</p>
</td></tr>
</table>
</body></html>"""


async def send_angebot_to_customer(
    *,
    angebot_id: UUID,
    to_email: str,
    cc: Optional[list[str]] = None,
) -> dict:
    """Versendet ein Lexware-Angebot per Microsoft-Graph an den Kunden.

    Returns: {success, error?, message_id?, conversation_id?, internet_message_id?}
    """
    out = {"success": False}

    # 1) Angebot + Tenant laden
    async with AsyncSessionLocal() as session:
        ang = (await session.execute(
            select(Angebot).where(Angebot.id == angebot_id)
        )).scalar_one_or_none()
        if ang is None:
            out["error"] = "Angebot nicht gefunden"
            return out
        tenant = (await session.execute(
            select(Tenant).where(Tenant.id == ang.tenant_id)
        )).scalar_one_or_none()
    if tenant is None:
        out["error"] = "Tenant nicht gefunden"
        return out
    if ang.lexware_quotation_id is None:
        out["error"] = "Angebot hat keine Lexware-Quotation-ID"
        return out

    # 2) Lexware-Provider aus der Tenant-ToolConfig laden (analog
    # _get_lexware_provider_for_tenant im telegram_notify-Handler — eine
    # frühere from_global_config()-Factory existierte nie wirklich, der
    # ursprüngliche Stub crashte beim ersten echten Aufruf 2026-05-12).
    provider = None
    async with AsyncSessionLocal() as session:
        tc = (await session.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == tenant.id,
                ToolConfig.tool_name == "lexware",
            )
        )).scalar_one_or_none()
    if tc and tc.enabled:
        cfg = tc.config or {}
        encrypted = cfg.get("encrypted_api_key")
        if encrypted:
            try:
                api_key = decrypt(encrypted)
                if api_key:
                    provider = LexwareProvider(api_key=api_key)
            except Exception as exc:
                logger.warning(f"Lexware-Key Entschluesselung gescheitert: {exc}")
    if provider is None:
        out["error"] = "Lexware ist nicht konfiguriert (siehe /lexware_setup)"
        return out

    try:
        quote = await provider.get_quotation(ang.lexware_quotation_id)
    except AccountingError as e:
        out["error"] = f"Lexware get_quotation: {e}"
        return out

    voucher_status = (quote.get("voucherStatus") or "").lower()
    if "draft" in voucher_status:
        out["error"] = (
            "Das Angebot ist in Lexware noch im Draft-Status. "
            "Bitte oeffne es im Lexware-Web und finalisiere es (Button 'Finalisieren'/'Ausstellen'). "
            "Danach erneut versenden."
        )
        return out

    # 3) PDF holen
    try:
        pdf_bytes = await provider.download_quotation_pdf(ang.lexware_quotation_id)
    except AccountingError as e:
        out["error"] = f"PDF-Download: {e}"
        return out
    except Exception as e:
        out["error"] = f"PDF-Download Fehler: {e}"
        return out

    # 4) Mail-Body bauen
    nummer = ang.lexware_voucher_number or quote.get("voucherNumber") or ""
    kunde_anrede = (ang.kunde_name or "").split()[-1] if ang.kunde_name else ""
    body_html = _build_angebot_mail_html(
        kunde_anrede=kunde_anrede,
        angebot_nummer=str(nummer),
        gesamtbetrag_brutto_eur=ang.gesamtbetrag_brutto_eur,
        company_name=tenant.company_name or "Handwerksbetrieb",
        contact_name=getattr(tenant, "contact_name", "") or "",
        contact_email=getattr(tenant, "contact_email", "") or "",
        contact_phone=getattr(tenant, "contact_phone", "") or "",
    )

    subject_nummer = f" {nummer}" if nummer else ""
    subject = f"Ihr Angebot{subject_nummer} von {tenant.company_name or 'uns'}"

    filename = f"Angebot{('-' + str(nummer)) if nummer else ''}.pdf"

    # 5) Two-Step-Send mit Tracking-IDs
    result = await send_tracked_mail(
        tenant_id=tenant.id,
        to_email=to_email,
        subject=subject,
        body_html=body_html,
        cc=cc,
        attachments=[{
            "filename": filename,
            "bytes": pdf_bytes,
            "content_type": "application/pdf",
        }],
    )
    if not result.get("success"):
        # Beta-1 B1-6: in failed_mail_queue legen statt verlieren.
        # Microsoft-Graph kann auch wegen invalid-grant kurz down sein —
        # der Retry-Cron probiert nach 5min, 30min, 2h erneut.
        out["error"] = result.get("error") or "Mail-Versand fehlgeschlagen"
        try:
            from core.integrations.mail_retry_cron import enqueue_failed_mail
            from core.models import (
                ANGEBOT_STATUS_MAIL_QUEUED, MAIL_TYPE_ANGEBOT,
            )
            async with AsyncSessionLocal() as session:
                ang_db = (await session.execute(
                    select(Angebot).where(Angebot.id == angebot_id)
                )).scalar_one_or_none()
                if ang_db is not None:
                    ang_db.kunde_email = to_email
                    ang_db.status = ANGEBOT_STATUS_MAIL_QUEUED
                    await session.commit()

            await enqueue_failed_mail(
                tenant_id=tenant.id,
                mail_type=MAIL_TYPE_ANGEBOT,
                recipient_email=to_email,
                subject=subject,
                html_body=body_html,
                attachments=[{
                    "filename": filename,
                    "mime_type": "application/pdf",
                    "content_bytes": pdf_bytes,
                }],
                from_name=tenant.company_name,
                angebot_id=str(angebot_id),
                mail_backend="microsoft_graph",
                last_error=out["error"],
            )
            out["queued"] = True
            logger.info(
                f"send_angebot_to_customer queued angebot={angebot_id} "
                f"to={to_email} reason={out['error'][:120]}"
            )
        except Exception as exc:
            logger.exception(f"enqueue_failed_mail (angebot) crashed: {exc}")
        return out

    # 6) Tracking-IDs am Angebot speichern
    async with AsyncSessionLocal() as session:
        ang_db = (await session.execute(
            select(Angebot).where(Angebot.id == angebot_id)
        )).scalar_one_or_none()
        if ang_db is not None:
            ang_db.kunde_email = to_email
            ang_db.mail_sent_to = to_email
            ang_db.mail_sent_at = dt.datetime.now(dt.timezone.utc)
            ang_db.mail_message_id = result.get("message_id")
            ang_db.mail_internet_message_id = result.get("internet_message_id")
            ang_db.mail_conversation_id = result.get("conversation_id")
            ang_db.status = "mail_sent"
            await session.commit()

    out["success"] = True
    out["message_id"] = result.get("message_id")
    out["internet_message_id"] = result.get("internet_message_id")
    out["conversation_id"] = result.get("conversation_id")
    logger.info(
        f"send_angebot_to_customer OK: angebot={angebot_id} to={to_email} "
        f"conv={(result.get('conversation_id') or '')[:30]}"
    )
    return out
