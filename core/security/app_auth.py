"""App-Auth fuer die Inhaber-/Mitarbeiter-PWA (``/app``).

Spiegelt das gehaertete Muster aus ``core/admin/auth.py``, aber:
- Identitaet ist ein ``Employee`` (kein eigenes User-System). ``is_default``
  == True = Inhaber, sonst Mitarbeiter.
- Login ist passwortlos via Magic-Link (``AppLoginToken``) — fuer die
  Handwerker-Zielgruppe reibungsaermer als Passwoerter.
- Session 30 Tage (App soll eingeloggt bleiben wie eine native App).

Sicherheits-Eigenschaften (wie Admin):
- Sessions als Server-side Records, Cookie traegt nur opaken Token.
- HTTP-Only, Secure (Prod), SameSite=Strict, Path=/app.
- CSRF-Token pro Session, gegen Header/Form validiert.
- Magic-Link-Anforderung rate-limited per IP.
- Strikte Tenant-Isolation: require_app_user liefert immer (Employee,
  Tenant); jede App-Query MUSS auf tenant.id scopen.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
import uuid
from typing import Optional

from fastapi import HTTPException, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from core.admin.auth import (  # bewaehrte Auth-Bausteine wiederverwenden
    _client_ip,  # IP-Extraktion respektiert Caddy-Header
    hash_password,
    verify_password,
)
from core.database.connection import get_session
from core.models.app_account import (
    APP_LOGIN_TOKEN_LIFETIME,
    APP_SESSION_LIFETIME,
    AppLoginToken,
    AppSession,
)
from core.models.employee import Employee
from core.models.tenant import Tenant

logger = logging.getLogger(__name__)


APP_SESSION_COOKIE_NAME = "ga_app_sid"
CSRF_FIELD_NAME = "_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
COOKIE_PATH = "/app"
COOKIE_SECURE = settings.is_production

# Magic-Link-Rate-Limit: max N Anforderungen pro IP pro Fenster.
LOGIN_RATE_WINDOW = dt.timedelta(minutes=15)
LOGIN_RATE_MAX = 5

# Sliding-Window: Session-Activity nur alle 5 Min in die DB schreiben.
_ACTIVITY_BUMP_SECONDS = 300


# =====================================================================
# MAGIC-LINK TOKEN
# =====================================================================

async def check_login_rate_limit(ip: str, *, session: AsyncSession) -> bool:
    """True wenn weitere Magic-Link-Anforderung erlaubt ist."""
    cutoff = dt.datetime.now(dt.timezone.utc) - LOGIN_RATE_WINDOW
    stmt = (
        select(AppLoginToken)
        .where(AppLoginToken.ip_address == ip)
        .where(AppLoginToken.created_at >= cutoff)
    )
    recent = (await session.execute(stmt)).scalars().all()
    return len(recent) < LOGIN_RATE_MAX


async def find_employee_by_email(
    email: str, *, session: AsyncSession,
) -> Optional[Employee]:
    """Aktiver Employee mit dieser Kontakt-Mail (case-insensitive)."""
    norm = email.strip().lower()
    if "@" not in norm or len(norm) > 255:
        return None
    stmt = (
        select(Employee)
        .where(Employee.is_active.is_(True))
        .where(func.lower(Employee.contact_email) == norm)
    )
    return (await session.execute(stmt)).scalars().first()


async def verify_app_login(
    email: str, password: str, *, session: AsyncSession,
) -> Optional[Employee]:
    """Klassisches E-Mail+Passwort-Login. Liefert den Employee oder None.

    None bei: keine Mail-Zuordnung, kein Passwort gesetzt, falsches Passwort,
    inaktiver Account (find_employee_by_email filtert is_active).
    """
    if not email or not password:
        return None
    emp = await find_employee_by_email(email, session=session)
    if emp is None or not emp.app_password_hash:
        return None
    if not verify_password(password, emp.app_password_hash):
        return None
    return emp


def set_app_password_hash(employee: Employee, password: str) -> None:
    """Setzt den bcrypt-Hash des PWA-Passworts auf einem Employee."""
    employee.app_password_hash = hash_password(password)


async def create_login_token(
    *, employee: Employee, request: Request, session: AsyncSession,
) -> AppLoginToken:
    """Erzeugt einen einmaligen Magic-Link-Token fuer einen Employee."""
    now = dt.datetime.now(dt.timezone.utc)
    tok = AppLoginToken(
        employee_id=employee.id,
        tenant_id=employee.tenant_id,
        token=secrets.token_urlsafe(40),
        expires_at=now + APP_LOGIN_TOKEN_LIFETIME,
        ip_address=_client_ip(request),
    )
    session.add(tok)
    await session.flush()
    return tok


async def consume_login_token(
    token: str, *, session: AsyncSession,
) -> Optional[tuple[Employee, Tenant]]:
    """Loest einen Magic-Link-Token atomar ein.

    Setzt ``used_at`` per bedingtem UPDATE (used_at IS NULL AND nicht
    abgelaufen) — race-sicher, ein Token kann nur EINMAL eingeloest werden.
    Liefert (Employee, Tenant) oder None.
    """
    if not token:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    res = await session.execute(
        update(AppLoginToken)
        .where(AppLoginToken.token == token)
        .where(AppLoginToken.used_at.is_(None))
        .where(AppLoginToken.expires_at > now)
        .values(used_at=now)
        .returning(AppLoginToken.employee_id, AppLoginToken.tenant_id)
    )
    row = res.first()
    if not row:
        return None
    employee_id, tenant_id = row
    emp = (await session.execute(
        select(Employee).where(Employee.id == employee_id)
    )).scalar_one_or_none()
    tenant = (await session.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    )).scalar_one_or_none()
    if emp is None or tenant is None or not emp.is_active:
        return None
    return emp, tenant


# =====================================================================
# SESSIONS
# =====================================================================

async def create_app_session(
    *, employee: Employee, request: Request, session: AsyncSession,
) -> AppSession:
    now = dt.datetime.now(dt.timezone.utc)
    sess = AppSession(
        employee_id=employee.id,
        tenant_id=employee.tenant_id,
        token=secrets.token_urlsafe(40),
        csrf_token=secrets.token_urlsafe(32),
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:500] or None,
        last_activity_at=now,
        expires_at=now + APP_SESSION_LIFETIME,
        revoked=False,
    )
    session.add(sess)
    await session.flush()
    return sess


def set_app_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=APP_SESSION_COOKIE_NAME,
        value=token,
        max_age=int(APP_SESSION_LIFETIME.total_seconds()),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path=COOKIE_PATH,
    )


def clear_app_session_cookie(response: Response) -> None:
    response.delete_cookie(APP_SESSION_COOKIE_NAME, path=COOKIE_PATH)


async def get_active_app_session(
    token: Optional[str], *, session: AsyncSession,
) -> Optional[tuple[AppSession, Employee, Tenant]]:
    """Liefert (Session, Employee, Tenant) wenn der Token gueltig ist."""
    if not token:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    stmt = (
        select(AppSession, Employee, Tenant)
        .join(Employee, AppSession.employee_id == Employee.id)
        .join(Tenant, AppSession.tenant_id == Tenant.id)
        .where(AppSession.token == token)
        .where(AppSession.revoked.is_(False))
        .where(AppSession.expires_at > now)
        .where(Employee.is_active.is_(True))
    )
    row = (await session.execute(stmt)).first()
    if not row:
        return None
    sess, emp, tenant = row
    if (now - sess.last_activity_at).total_seconds() > _ACTIVITY_BUMP_SECONDS:
        sess.last_activity_at = now
        sess.expires_at = now + APP_SESSION_LIFETIME
    return sess, emp, tenant


async def revoke_app_session(token: str, *, session: AsyncSession) -> None:
    await session.execute(
        update(AppSession).where(AppSession.token == token).values(revoked=True)
    )


# =====================================================================
# DEPENDENCIES (FastAPI)
# =====================================================================

class _AppRedirect(HTTPException):
    """303-Redirect zur App-Login-Seite (vom Exception-Handler gerendert)."""
    pass


async def require_app_user(request: Request) -> Employee:
    """FastAPI-Dependency: laedt Session aus Cookie, validiert, returnet Employee.

    Stasht Session/Employee/Tenant/CSRF in request.state fuer Handler +
    Template. Bei fehlender Session: 303 Redirect zu /app/login.
    """
    token = request.cookies.get(APP_SESSION_COOKIE_NAME)
    async with get_session() as s:
        result = await get_active_app_session(token, session=s)
        if not result:
            raise _AppRedirect(
                status_code=303, detail="Login erforderlich",
                headers={"Location": "/app/login"},
            )
        sess, emp, tenant = result
        request.state.app_session = sess
        request.state.app_employee = emp
        request.state.app_tenant = tenant
        request.state.app_csrf = sess.csrf_token
        request.state.app_is_inhaber = bool(emp.is_default)
    return emp


async def require_app_inhaber(request: Request) -> Employee:
    """Wie require_app_user, erzwingt aber Inhaber (is_default)."""
    emp = await require_app_user(request)
    if not emp.is_default:
        raise HTTPException(403, "Nur der Inhaber darf das.")
    return emp


async def require_app_csrf(request: Request) -> None:
    """Validiert CSRF aus Header (JSON-API) oder Form gegen die Session.

    require_app_user() muss zuerst gelaufen sein.
    """
    sess: Optional[AppSession] = getattr(request.state, "app_session", None)
    if not sess:
        raise HTTPException(403, "Keine Session")
    posted = request.headers.get(CSRF_HEADER_NAME)
    if not posted:
        try:
            form = await request.form()
            posted = form.get(CSRF_FIELD_NAME)
        except Exception:
            posted = None
    if not posted or not secrets.compare_digest(str(posted), sess.csrf_token):
        raise HTTPException(403, "Ungueltiges CSRF-Token")


def current_tenant_id(request: Request) -> uuid.UUID:
    """Tenant-ID der aktuellen Session — fuer hartes Query-Scoping."""
    tenant: Optional[Tenant] = getattr(request.state, "app_tenant", None)
    if tenant is None:
        raise HTTPException(401, "Keine App-Session")
    return tenant.id
