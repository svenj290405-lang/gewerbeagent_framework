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

from config.settings import settings
from core.admin.auth import audit, require_admin, require_csrf
from core.admin.routes import templates  # gemeinsame Jinja2-Instanz
from core.database import AsyncSessionLocal
from core.database.connection import get_session
from core.models.admin import AdminUser
from core.onboarding import (
    OnboardingError,
    create_owner_activation,
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


def _onboarding_mail_bodies(company: str, contact: str, activate_url: str,
                            sender_name: str) -> tuple[str, str]:
    """Liefert (html, text) fuer die Begruessungs-Mail.

    Ein Link in die App: Passwort setzen, fertig eingeloggt. Frueher lief
    das ueber einen Telegram-Bot und einen kurzen Code — der Bot ist am
    2026-08-21 aus DSGVO-Gruenden entfallen.
    """
    html = (
        f"<p>Hallo {contact or 'und herzlich willkommen'},</p>"
        f"<p>Ihr Zugang fuer <b>{company}</b> ist eingerichtet — die "
        "Einrichtung dauert ca. 2 Minuten.</p>"
        "<p><b>So geht's:</b></p>"
        "<ol>"
        "<li>Auf den Knopf unten tippen (am besten am Handy).</li>"
        "<li>E-Mail bestaetigen und ein Passwort vergeben.</li>"
        "<li>Fertig — Sie sind sofort in Ihrer App.</li>"
        "</ol>"
        f"<p style=\"margin:22px 0\"><a href=\"{activate_url}\" "
        "style=\"background:#111;color:#fff;padding:13px 22px;"
        "border-radius:8px;text-decoration:none;font-weight:600\">"
        "Zugang einrichten</a></p>"
        f"<p style=\"color:#666;font-size:13px\">Falls der Knopf nicht geht: "
        f"<a href=\"{activate_url}\">{activate_url}</a></p>"
        "<p style=\"color:#666;font-size:13px\">Der Link ist persoenlich, nur "
        "einmal verwendbar und 14 Tage gueltig. Bitte nicht weitergeben.</p>"
        f"<p>Viele Gruesse<br>{sender_name}</p>"
    )
    text = (
        f"Hallo {contact or ''},\n\n"
        f"Ihr Zugang fuer {company} ist eingerichtet — die Einrichtung "
        "dauert ca. 2 Minuten.\n\n"
        "1. Diesen Link oeffnen (am besten am Handy):\n"
        f"   {activate_url}\n"
        "2. E-Mail bestaetigen und ein Passwort vergeben.\n"
        "3. Fertig — Sie sind sofort in Ihrer App.\n\n"
        "Der Link ist persoenlich, nur einmal verwendbar und 14 Tage "
        f"gueltig. Bitte nicht weitergeben.\n\nViele Gruesse\n{sender_name}"
    )
    return html, text


async def _send_onboarding_mail(*, to_email: str, to_name: str,
                                company: str, activate_url: str) -> None:
    """Schickt die Begruessungs-Mail mit dem Aktivierungs-Link.

    Wirft eine Exception bei Fehler — der Aufrufer faengt sie und meldet,
    dass der Tenant zwar angelegt, die Mail aber nicht raus ist (der Link
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
        company, to_name, activate_url, "Gewerbeagent",
    )
    res = await send_tracked_mail(
        tenant_id=gid,
        to_email=to_email,
        subject="Ihr Zugang zu Gewerbeagent — Einrichtung in 2 Minuten",
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
    # Neuer Starlette-Stil (request als erstes Argument). Der alte Aufruf
    # warf "TypeError: unhashable type: 'dict'" — diese Seite war damit tot
    # und es liess sich KEIN neuer Betrieb anlegen (Audit 2026-08-24). Alle
    # anderen Admin-Seiten waren am 23.08. umgestellt worden, diese eine
    # blieb uebrig, weil kein Rauchtest sie abdeckte.
    return templates.TemplateResponse(request, "tenant_new.html", {
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

    # Sicheren Aktivierungs-Token erzeugen.
    token_obj = None
    try:
        token_obj = await create_owner_activation(
            result.tenant_id, result.default_employee_id,
        )
    except Exception:
        logger.exception("Aktivierungs-Token-Erzeugung fehlgeschlagen")

    activate_url = (
        f"{settings.app_url}/app/activate?token={token_obj.token}"
        if token_obj is not None else ""
    )

    # Optional: Einladungs-Mail an den Kunden.
    mail_note = ""
    if (send_mail or "").strip() and token_obj is not None:
        try:
            await _send_onboarding_mail(
                to_email=contact_email.strip(),
                to_name=contact_name.strip(),
                company=company_name.strip(),
                activate_url=activate_url,
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
                "link_ok": bool(token_obj),
            },
        )
        await s.commit()

    # Ergebnis zurueck aufs Formular (zeigt Link + Mail-Status, damit der
    # Betreiber den Link auch bei Mail-Fehler kopieren kann).
    parts = [f"Betrieb '{slug.strip().lower()}' angelegt."]
    if mail_note:
        parts.append(mail_note)
    if activate_url:
        parts.append(f"Aktivierungs-Link: {activate_url}")
    else:
        parts.append("ACHTUNG: Aktivierungs-Link konnte nicht erzeugt werden.")
    return RedirectResponse(
        f"/admin/tenants/new?msg={quote('  ·  '.join(parts))}",
        status_code=303,
    )
