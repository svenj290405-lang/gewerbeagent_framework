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

from fastapi import Depends, HTTPException, Request, Response
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

# Passwort-Login-Brute-Force-Schutz: fehlgeschlagene Versuche pro IP.
# Bewusst prozessintern (kein DB-Marker) — die App laeuft als EIN uvicorn-
# Prozess ohne --workers, darum ist der In-Memory-Zaehler vollstaendig wirksam
# und ueberlebt bewusst keinen Neustart (dann ist die Sperre eh hinfaellig).
# Anders als beim Magic-Link koennen wir hier keinen AppLoginToken-Marker
# anlegen: dessen employee_id/tenant_id sind NOT NULL, bei unbekannter Mail
# gibt es aber keinen Employee.
FAILED_LOGIN_WINDOW = dt.timedelta(minutes=15)
FAILED_LOGIN_MAX = 10
_failed_logins: dict[str, list[dt.datetime]] = {}

# Sliding-Window: Session-Activity nur alle 5 Min in die DB schreiben.
_ACTIVITY_BUMP_SECONDS = 300


def _prune_failed_logins(ip: str, now: dt.datetime) -> list[dt.datetime]:
    cutoff = now - FAILED_LOGIN_WINDOW
    kept = [t for t in _failed_logins.get(ip, ()) if t >= cutoff]
    if kept:
        _failed_logins[ip] = kept
    else:
        _failed_logins.pop(ip, None)
    return kept


def password_login_locked(ip: str) -> bool:
    """True wenn diese IP zu viele Passwort-Fehlversuche im Fenster hatte."""
    now = dt.datetime.now(dt.timezone.utc)
    return len(_prune_failed_logins(ip, now)) >= FAILED_LOGIN_MAX


def record_failed_password_login(ip: str) -> None:
    """Vermerkt einen fehlgeschlagenen Passwort-Login fuer die IP-Sperre."""
    now = dt.datetime.now(dt.timezone.utc)
    kept = _prune_failed_logins(ip, now)
    # Speicher-Deckel: der Zaehler zaehlt bis MAX, mehr braucht die Sperre nicht.
    if len(kept) <= FAILED_LOGIN_MAX * 2:
        _failed_logins.setdefault(ip, []).append(now)


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
    # Aktivitaets-Tracking (failsafe, eigene Session) — jeder Login zaehlt.
    from core.models.app_usage_event import record_app_usage, USAGE_LOGIN
    await record_app_usage(employee.tenant_id, employee.id, USAGE_LOGIN)
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

    Idempotent innerhalb eines Requests: das Router-Gate
    (enforce_app_permission) ruft die Funktion direkt auf, der Endpunkt
    danach nochmal per Depends. FastAPIs Dependency-Cache greift bei dem
    direkten Aufruf nicht — deshalb hier ein eigener Kurzschluss, sonst
    liefe pro Request eine zweite Session-Query.
    """
    vorhanden = getattr(request.state, "app_employee", None)
    if vorhanden is not None:
        return vorhanden

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

    # Effektive Rechte einmal pro Request aufloesen (60s-Cache dahinter),
    # damit Handler, Router-Gate und /api/me dieselbe Menge sehen.
    from core.features.permission_check import rechte_fuer_employee
    request.state.app_permissions = await rechte_fuer_employee(emp)
    return emp


async def require_app_inhaber(request: Request) -> Employee:
    """Wie require_app_user, erzwingt aber Inhaber (is_default)."""
    emp = await require_app_user(request)
    if not emp.is_default:
        raise HTTPException(403, "Nur der Inhaber darf das.")
    return emp


def current_permissions(request: Request) -> frozenset[str]:
    """Effektive Rechte der laufenden Session.

    Leer, wenn require_app_user noch nicht lief — fail-closed, damit ein
    falsch verdrahteter Handler nichts durchlaesst.
    """
    return getattr(request.state, "app_permissions", frozenset())


# Schalter fuer die Rechte-Durchsetzung.
#
# False = Trockenlauf: es wird nur geloggt, nichts geblockt. Ablesen mit
#
#   docker logs gewerbeagent_framework | grep "Recht fehlt" | sort | uniq -c
#
# Seit 2026-08-19 auf True. Der Trockenlauf hatte keine einzige Zeile
# geliefert (ausser dem Inhaber war nie jemand eingeloggt), darum ist die
# Absicherung stattdessen durchgespielt worden: alle 122 Endpunkte gegen
# alle drei Rollen, dazu die Zeilen-Sichtbarkeit bei Auftraegen und die
# Q-Tools. Der Rueckweg bleibt diese eine Zeile plus Restart.
PERMISSIONS_DURCHSETZEN = True


async def enforce_app_permission(request: Request) -> None:
    """Router-weites Rechte-Gate fuer /app und /app/api.

    Bewusst OHNE ``Depends(require_app_user)`` in der Signatur: eine
    solche Dependency liefe VOR dem Funktionskoerper und wuerde damit
    auch fuer die oeffentlichen Endpunkte (Login, Aktivierung, Manifest)
    eine Session verlangen — die Login-Seite waere unerreichbar. Die
    Session wird darum erst geholt, wenn feststeht, dass der Endpunkt
    eine braucht. ``require_app_user`` ist innerhalb eines Requests
    idempotent, es entsteht also keine zweite Query.

    Fail-closed: ein Endpunkt, der nicht in ROUTE_RECHTE steht, wird
    abgelehnt. Eine neue Route ist damit tot, bis sie eingetragen ist —
    das merkt man in Sekunden. Ein stilles Loch merkt man nie.
    """
    from core.security.app_permission_routes import (
        OEFFENTLICHE_ENDPUNKTE, OFFEN, recht_fuer_endpunkt,
    )

    endpoint = request.scope.get("endpoint")
    name = getattr(endpoint, "__name__", None)
    # Kein aufloesbarer Endpunkt (z.B. StaticFiles-Mount) oder bewusst
    # oeffentlich -> das Gate ist nicht zustaendig.
    if name is None or name in OEFFENTLICHE_ENDPUNKTE:
        return

    # Ab hier ist eine Session Pflicht (auch fuer OFFEN-Endpunkte —
    # "offen" heisst "jeder EINGELOGGTE", nicht "jeder").
    emp = await require_app_user(request)
    emp_slug = getattr(emp, "slug", "?")

    recht = recht_fuer_endpunkt(name)
    if recht == OFFEN:
        return

    if recht is None:
        logger.error(
            "Endpunkt %r fehlt in ROUTE_RECHTE — %s. "
            "Eintrag in core/security/app_permission_routes.py ergaenzen.",
            name,
            "abgelehnt" if PERMISSIONS_DURCHSETZEN else "im Trockenlauf durchgelassen",
        )
        if PERMISSIONS_DURCHSETZEN:
            raise HTTPException(403, "Diese Funktion ist nicht freigegeben.")
        return

    if recht in current_permissions(request):
        return

    if not PERMISSIONS_DURCHSETZEN:
        logger.warning(
            "Recht fehlt (Trockenlauf): emp=%s route=%s recht=%s",
            emp_slug, name, recht,
        )
        return

    logger.info(
        "Recht fehlt: emp=%s route=%s recht=%s", emp_slug, name, recht,
    )
    raise HTTPException(
        403,
        "Dafür fehlt dir die Berechtigung. Der Inhaber kann sie freigeben.",
    )


def require_app_permission(key: str):
    """Dependency-Factory: verlangt ein einzelnes Recht.

    Wird ab der Durchsetzungs-Stufe von der zentralen Routen-Tabelle
    genutzt; einzeln einsetzbar fuer Endpunkte ausserhalb von
    /app/api (z.B. in app_routes.py).
    """
    async def _dep(request: Request) -> Employee:
        emp = await require_app_user(request)
        if key not in current_permissions(request):
            raise HTTPException(
                403,
                "Dafür fehlt dir die Berechtigung. "
                "Der Inhaber kann sie freigeben.",
            )
        return emp

    return _dep


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
