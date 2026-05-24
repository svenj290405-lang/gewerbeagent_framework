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
from core.models import format_short_code
from core.onboarding import (
    OnboardingError,
    create_owner_activation,
    create_tenant_record,
    global_bot_username,
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


def _onboarding_mail_bodies(company: str, contact: str, code: str,
                            bot_username: str | None,
                            sender_name: str) -> tuple[str, str]:
    """Liefert (html, text) fuer die Begruessungs-Mail — Code-basiert, OHNE
    Deep-Link (Links sind in Telegram Web unzuverlaessig). Der Kunde sucht
    den Bot, drueckt START und tippt den kurzen Code."""
    bot = f"@{bot_username}" if bot_username else "unseren Telegram-Bot"
    pretty = format_short_code(code)
    html = (
        f"<p>Hallo {contact or 'und herzlich willkommen'},</p>"
        f"<p>Ihr Zugang fuer <b>{company}</b> ist eingerichtet. Die "
        "Verbindung laeuft ueber Telegram — die Einrichtung dauert ca. "
        "5 Minuten.</p>"
        "<p><b>So geht's:</b></p>"
        "<ol>"
        "<li>Telegram am Handy oder PC oeffnen.</li>"
        f"<li>Oben in die <b>Suche</b> <b>{bot}</b> eingeben und den Bot oeffnen.</li>"
        "<li>Auf <b>START</b> tippen.</li>"
        "<li>Diesen <b>Aktivierungs-Code</b> eingeben:</li>"
        "</ol>"
        "<p style=\"font-size:26px;font-weight:700;letter-spacing:2px;"
        f"font-family:monospace;margin:18px 0\">{pretty}</p>"
        "<p style=\"color:#666;font-size:13px\">Der Code ist persoenlich, nur "
        "einmal verwendbar und 14 Tage gueltig. Bitte nicht weitergeben.</p>"
        f"<p>Viele Gruesse<br>{sender_name}</p>"
    )
    text = (
        f"Hallo {contact or ''},\n\n"
        f"Ihr Zugang fuer {company} ist eingerichtet. Die Verbindung laeuft "
        "ueber Telegram, die Einrichtung dauert ca. 5 Minuten.\n\n"
        "1. Telegram oeffnen.\n"
        f"2. In der Suche {bot} eingeben und den Bot oeffnen.\n"
        "3. Auf START tippen.\n"
        f"4. Diesen Aktivierungs-Code eingeben: {pretty}\n\n"
        "Der Code ist persoenlich, nur einmal verwendbar und 14 Tage "
        f"gueltig. Bitte nicht weitergeben.\n\nViele Gruesse\n{sender_name}"
    )
    return html, text


async def _send_onboarding_mail(*, to_email: str, to_name: str,
                                company: str, code: str,
                                bot_username: str | None) -> None:
    """Schickt die Begruessungs-Mail mit dem Aktivierungs-Code.

    Wirft eine Exception bei Fehler — der Aufrufer faengt sie und meldet,
    dass der Tenant zwar angelegt, die Mail aber nicht raus ist (der Code
    wird im Admin-Ergebnis trotzdem angezeigt und kann manuell geschickt
    werden).
    """
    from sqlalchemy import select
    from core.models import Tenant, OAuthToken
    from core.integrations.microsoft import send_tracked_mail

    # Versand ueber das _global-Outlook-Plattformpostfach (Microsoft Graph) —
    # Brevo lieferte nicht zuverlaessig, Outlook ist erprobt. Das _global-
    # Postfach wird vom Inbox-Poller ausgenommen (Send-only).
    async with AsyncSessionLocal() as s:
        gt = (await s.execute(
            select(Tenant).where(Tenant.slug == GLOBAL_TENANT_SLUG)
        )).scalar_one_or_none()
        tok = None
        if gt is not None:
            tok = (await s.execute(
                select(OAuthToken).where(
                    OAuthToken.tenant_id == gt.id,
                    OAuthToken.provider == "microsoft",
                )
            )).scalar_one_or_none()
        if gt is None or tok is None:
            raise RuntimeError(
                "Kein _global-Outlook-Postfach verbunden — Microsoft-OAuth "
                "fuer _global fehlt (Plattform-Mailversand nicht konfiguriert)."
            )
        gid, eid = gt.id, tok.employee_id

    html, _text = _onboarding_mail_bodies(
        company, to_name, code, bot_username, "Gewerbeagent",
    )
    res = await send_tracked_mail(
        tenant_id=gid,
        to_email=to_email,
        subject="Ihr Zugang zu Gewerbeagent — Einrichtung in 5 Minuten",
        body_html=html,
        employee_id=eid,
    )
    if not res.get("success"):
        raise RuntimeError(f"Outlook-Versand fehlgeschlagen: {res.get('error')}")


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

    # Sicheren Aktivierungs-Token (mit kurzem Code) erzeugen.
    token_obj = None
    code_pretty: str | None = None
    try:
        token_obj = await create_owner_activation(
            result.tenant_id, result.default_employee_id,
        )
        code_pretty = format_short_code(token_obj.short_code)
    except Exception:
        logger.exception("Aktivierungs-Token-Erzeugung fehlgeschlagen")

    bot_username = await global_bot_username()

    # Optional: Code-Mail an den Kunden (ohne Deep-Link).
    mail_note = ""
    if (send_mail or "").strip() and token_obj is not None:
        try:
            await _send_onboarding_mail(
                to_email=contact_email.strip(),
                to_name=contact_name.strip(),
                company=company_name.strip(),
                code=token_obj.short_code,
                bot_username=bot_username,
            )
            mail_note = f"Begruessungs-Mail an {contact_email.strip()} verschickt."
        except Exception as e:
            logger.exception("Onboarding-Mail-Versand fehlgeschlagen")
            mail_note = (
                f"ACHTUNG: Mail-Versand fehlgeschlagen ({e}). "
                "Code bitte manuell schicken."
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
                "code_ok": bool(code_pretty),
            },
        )
        await s.commit()

    # Ergebnis zurueck aufs Formular (zeigt Link + Mail-Status, damit der
    # Betreiber den Link auch bei Mail-Fehler kopieren kann).
    parts = [f"Betrieb '{slug.strip().lower()}' angelegt."]
    if mail_note:
        parts.append(mail_note)
    if code_pretty:
        bot = f"@{bot_username}" if bot_username else "den Bot"
        parts.append(
            f"Aktivierungs-Code: {code_pretty}  ({bot} in Telegram suchen "
            "→ START → Code eingeben)"
        )
    else:
        parts.append("ACHTUNG: Aktivierungs-Code konnte nicht erzeugt werden.")
    return RedirectResponse(
        f"/admin/tenants/new?msg={quote('  ·  '.join(parts))}",
        status_code=303,
    )
