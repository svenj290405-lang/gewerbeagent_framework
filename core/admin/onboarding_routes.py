"""Admin-UI: neuen Betrieb (Tenant) anlegen + sicheren Onboarding-Link
per Mail verschicken.

Ersetzt fuer den Normalfall das CLI-Skript scripts/onboard.py: der
Betreiber gibt im Admin-Tool ein paar Eckdaten ein, der Tenant wird
angelegt und der Kunde bekommt automatisch eine Mail mit seinem
persoenlichen, einmaligen Aktivierungs-Link (S13 — kein ratbarer
Slug-Link mehr). Die uebrigen Schritte (sipgate-Nummer, ElevenLabs-
Agent, Kalender-OAuth) bleiben wie gehabt separat.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from core.admin.auth import audit, require_admin, require_csrf
from core.admin.routes import templates  # gemeinsame Jinja2-Instanz
from core.database import AsyncSessionLocal
from core.database.connection import get_session
from core.models.admin import AdminUser
from core.onboarding import (
    OnboardingError,
    build_owner_activation_link,
    create_tenant_record,
    list_available_branches,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-onboarding"])

GLOBAL_TENANT_SLUG = "_global"


async def _load_central_brevo_config() -> dict | None:
    """brevo_api_key + sender_email/name aus der zentralen _global/
    mail_intake ToolConfig (gleicher Speicherort wie der Outbound-
    Mailversand in core/integrations/mail_retry_cron.py)."""
    from core.models import Tenant, ToolConfig
    async with AsyncSessionLocal() as s:
        tc = (await s.execute(
            select(ToolConfig)
            .join(Tenant, ToolConfig.tenant_id == Tenant.id)
            .where(Tenant.slug == GLOBAL_TENANT_SLUG)
            .where(ToolConfig.tool_name == "mail_intake")
        )).scalar_one_or_none()
        return (tc.config if tc else None) or None


def _onboarding_mail_bodies(company: str, contact: str, link: str,
                            sender_name: str) -> tuple[str, str]:
    """Liefert (html, text) fuer die Begruessungs-Mail."""
    html = (
        f"<p>Hallo {contact or 'und herzlich willkommen'},</p>"
        f"<p>Ihr Zugang fuer <b>{company}</b> ist eingerichtet. Die "
        "Verbindung laeuft komplett ueber Telegram — die Einrichtung "
        "dauert ca. 5 Minuten.</p>"
        "<p><b>So geht's:</b></p>"
        "<ol>"
        "<li>Telegram auf dem Handy installieren (falls noch nicht da).</li>"
        "<li>Auf den folgenden persoenlichen Button tippen:</li>"
        "</ol>"
        f"<p style=\"margin:24px 0\"><a href=\"{link}\" "
        "style=\"background:#2563eb;color:#fff;padding:12px 22px;"
        "border-radius:8px;text-decoration:none;font-weight:600\">"
        "Jetzt verbinden &amp; einrichten</a></p>"
        "<p>Falls der Button nicht funktioniert, diesen Link in Telegram "
        f"oeffnen:<br><a href=\"{link}\">{link}</a></p>"
        "<p style=\"color:#666;font-size:13px\">Der Link ist persoenlich, "
        "nur einmal verwendbar und 14 Tage gueltig. Bitte nicht "
        "weitergeben.</p>"
        f"<p>Viele Gruesse<br>{sender_name}</p>"
    )
    text = (
        f"Hallo {contact or ''},\n\n"
        f"Ihr Zugang fuer {company} ist eingerichtet. Die Verbindung laeuft "
        "ueber Telegram, die Einrichtung dauert ca. 5 Minuten.\n\n"
        "1. Telegram auf dem Handy installieren (falls noch nicht da).\n"
        "2. Diesen persoenlichen Link in Telegram oeffnen:\n\n"
        f"{link}\n\n"
        "Der Link ist persoenlich, nur einmal verwendbar und 14 Tage "
        f"gueltig. Bitte nicht weitergeben.\n\nViele Gruesse\n{sender_name}"
    )
    return html, text


async def _send_onboarding_mail(*, to_email: str, to_name: str,
                                company: str, link: str) -> None:
    """Schickt die Begruessungs-Mail mit dem Aktivierungs-Link.

    Wirft eine Exception bei Fehler — der Aufrufer faengt sie und meldet,
    dass der Tenant zwar angelegt, die Mail aber nicht raus ist (der Link
    wird im Admin-Ergebnis trotzdem angezeigt und kann manuell geschickt
    werden).
    """
    from core.integrations.brevo import BrevoMailer, MailRecipient
    cfg = await _load_central_brevo_config()
    api_key = (cfg or {}).get("brevo_api_key", "")
    sender_email = (cfg or {}).get("sender_email", "")
    sender_name = (cfg or {}).get("sender_name", "Gewerbeagent")
    if not api_key or not sender_email:
        raise RuntimeError(
            "Brevo-Config (_global/mail_intake) unvollstaendig — "
            "kein api_key/sender_email."
        )
    html, text = _onboarding_mail_bodies(company, to_name, link, sender_name)
    mailer = BrevoMailer(api_key=api_key)
    await mailer.send(
        sender_email=sender_email,
        sender_name=sender_name,
        to=MailRecipient(email=to_email, name=to_name or to_email),
        subject=f"Ihr Zugang zu {sender_name} — Einrichtung in 5 Minuten",
        html_body=html,
        text_body=text,
    )


@router.get("/tenants/new", response_class=HTMLResponse)
async def tenant_new_form(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    """Formular: neuen Betrieb anlegen."""
    return templates.TemplateResponse("tenant_new.html", {
        "request": request,
        "user": user,
        "active": "tenants",
        "csrf_token": request.state.admin_csrf,
        "branches": list_available_branches(),
        "msg": request.query_params.get("msg"),
        "err": request.query_params.get("err"),
    })


@router.post("/tenants/new")
async def tenant_new_submit(
    request: Request,
    slug: str = Form(...),
    company_name: str = Form(...),
    contact_name: str = Form(...),
    contact_email: str = Form(...),
    contact_phone: str = Form(""),
    branche: str = Form(""),
    send_mail: str = Form(""),
    user: AdminUser = Depends(require_admin),
):
    """Legt den Betrieb an, erzeugt den sicheren Onboarding-Link und
    schickt ihn (optional) per Mail an den Kunden."""
    await require_csrf(request)

    try:
        result = await create_tenant_record(
            slug=slug,
            name=company_name,
            email=contact_email,
            contact=contact_name,
            phone=contact_phone or None,
            branche=branche or None,
        )
    except OnboardingError as e:
        # Fachlicher Fehler (Slug vergeben/ungueltig, Pflichtfeld leer) —
        # zurueck zum Formular mit Meldung.
        return RedirectResponse(
            f"/admin/tenants/new?err={quote(str(e))}", status_code=303,
        )

    # Sicheren Inhaber-Onboarding-Link erzeugen (Aktivierungs-Token).
    link: str | None = None
    try:
        link = await build_owner_activation_link(
            result.tenant_id, result.default_employee_id,
        )
    except Exception:
        logger.exception("Onboarding-Link-Erzeugung fehlgeschlagen")

    # Optional: Mail an den Kunden.
    mail_note = ""
    if (send_mail or "").strip() and link:
        try:
            await _send_onboarding_mail(
                to_email=contact_email.strip(),
                to_name=contact_name.strip(),
                company=company_name.strip(),
                link=link,
            )
            mail_note = f"Begruessungs-Mail an {contact_email.strip()} verschickt."
        except Exception as e:
            logger.exception("Onboarding-Mail-Versand fehlgeschlagen")
            mail_note = (
                f"ACHTUNG: Mail-Versand fehlgeschlagen ({e}). "
                "Link bitte manuell schicken."
            )

    # Audit-Eintrag.
    async with get_session() as s:
        await audit(
            user_id=user.id,
            action="tenant.onboard.create",
            target=slug.strip().lower(),
            request=request,
            session=s,
            details={
                "company": company_name.strip(),
                "mailed": bool(mail_note and "fehlgeschlagen" not in mail_note),
                "link_ok": bool(link),
            },
        )
        await s.commit()

    # Ergebnis zurueck aufs Formular (zeigt Link + Mail-Status, damit der
    # Betreiber den Link auch bei Mail-Fehler kopieren kann).
    parts = [f"Betrieb '{slug.strip().lower()}' angelegt."]
    if mail_note:
        parts.append(mail_note)
    if link:
        parts.append(f"Onboarding-Link: {link}")
    else:
        parts.append("ACHTUNG: Onboarding-Link konnte nicht erzeugt werden.")
    return RedirectResponse(
        f"/admin/tenants/new?msg={quote('  ·  '.join(parts))}",
        status_code=303,
    )
