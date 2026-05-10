"""
Admin-Auth: Login, Sessions, CSRF, Rate-Limiting, Audit-Log.

Sicherheits-Setup:
- Passwoerter via bcrypt (cost=12)
- Sessions als Server-side Records (Cookie traegt nur opaken Token)
- HTTP-Only, Secure, SameSite=Strict Cookies
- CSRF-Token in Form-Hidden-Field, gegen Session-csrf_token validiert
- Rate-Limit /admin/login: max 5 Versuche / IP / 15min
- 24h Inaktivitaets-Timeout
- Audit-Log fuer alle relevanten Aktionen
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
import uuid
from typing import Optional

import bcrypt
from fastapi import Cookie, HTTPException, Request, Response
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from core.database.connection import get_session
from core.models.admin import (
    AdminAuditLog,
    AdminLoginAttempt,
    AdminSession,
    AdminUser,
)

logger = logging.getLogger(__name__)


SESSION_COOKIE_NAME = "ga_admin_sid"
CSRF_FIELD_NAME = "_csrf"
SESSION_LIFETIME = dt.timedelta(hours=24)
LOGIN_RATE_WINDOW = dt.timedelta(minutes=15)
LOGIN_RATE_MAX = 5
BCRYPT_ROUNDS = 12

# In-Production wird das Cookie nur via HTTPS uebermittelt.
COOKIE_SECURE = settings.is_production


# =====================================================================
# PASSWORD HASHING
# =====================================================================

def hash_password(plain: str) -> str:
    """bcrypt-Hash mit Cost-Factor 12."""
    if not plain:
        raise ValueError("Passwort darf nicht leer sein")
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Konstanteit-Check via bcrypt.checkpw."""
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# =====================================================================
# AUDIT LOG
# =====================================================================

async def audit(
    *,
    user_id: Optional[uuid.UUID] = None,
    action: str,
    target: Optional[str] = None,
    request: Optional[Request] = None,
    success: bool = True,
    details: Optional[dict] = None,
    session: Optional[AsyncSession] = None,
) -> None:
    """Schreibt einen Audit-Log-Eintrag. Failsafe."""
    try:
        ip = None
        ua = None
        if request:
            ip = _client_ip(request)
            ua = request.headers.get("user-agent")[:500] if request.headers.get("user-agent") else None

        row = AdminAuditLog(
            user_id=user_id,
            action=action[:80],
            target=(target or "")[:255] or None,
            ip_address=ip,
            user_agent=ua,
            success=success,
            details=details,
        )
        if session is not None:
            session.add(row)
        else:
            async with get_session() as s:
                s.add(row)
    except Exception as e:
        logger.warning(f"audit log failed: {e}")


def _client_ip(request: Request) -> str:
    """X-Real-IP / X-Forwarded-For respektieren (Caddy setzt diese)."""
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.split(",")[0].strip()[:64]
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


# =====================================================================
# RATE LIMITING
# =====================================================================

async def check_login_rate_limit(ip: str, *, session: AsyncSession) -> bool:
    """True wenn Login erlaubt, False wenn Rate-Limit getroffen."""
    cutoff = dt.datetime.now(dt.timezone.utc) - LOGIN_RATE_WINDOW
    stmt = (
        select(AdminLoginAttempt)
        .where(AdminLoginAttempt.ip_address == ip)
        .where(AdminLoginAttempt.attempted_at >= cutoff)
        .where(AdminLoginAttempt.success.is_(False))
    )
    failed = (await session.execute(stmt)).scalars().all()
    return len(failed) < LOGIN_RATE_MAX


async def record_login_attempt(
    *, ip: str, email: str | None, success: bool, session: AsyncSession
) -> None:
    session.add(
        AdminLoginAttempt(
            ip_address=ip[:64], email_tried=(email or "")[:255] or None,
            success=success,
        )
    )


# =====================================================================
# SESSIONS
# =====================================================================

async def create_session(
    *, user: AdminUser, request: Request, session: AsyncSession,
) -> AdminSession:
    now = dt.datetime.now(dt.timezone.utc)
    sess = AdminSession(
        user_id=user.id,
        token=secrets.token_urlsafe(40),
        csrf_token=secrets.token_urlsafe(32),
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:500] or None,
        last_activity_at=now,
        expires_at=now + SESSION_LIFETIME,
        revoked=False,
    )
    session.add(sess)
    return sess


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/admin",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/admin")


async def get_active_session(
    token: Optional[str], *, session: AsyncSession,
) -> Optional[tuple[AdminSession, AdminUser]]:
    """Liefert (Session, User) wenn Token gueltig, sonst None."""
    if not token:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    stmt = (
        select(AdminSession, AdminUser)
        .join(AdminUser, AdminSession.user_id == AdminUser.id)
        .where(AdminSession.token == token)
        .where(AdminSession.revoked.is_(False))
        .where(AdminSession.expires_at > now)
        .where(AdminUser.is_active.is_(True))
    )
    row = (await session.execute(stmt)).first()
    if not row:
        return None
    sess, user = row
    # 24h Inaktivitaet
    if (now - sess.last_activity_at) > SESSION_LIFETIME:
        sess.revoked = True
        return None
    # Activity bumpen + neuen Expiry setzen (Sliding-Window light:
    # nur alle 5min damit wir nicht jeden Request DB schreiben)
    if (now - sess.last_activity_at).total_seconds() > 300:
        sess.last_activity_at = now
        sess.expires_at = now + SESSION_LIFETIME
    return sess, user


async def revoke_session(token: str, *, session: AsyncSession) -> None:
    await session.execute(
        update(AdminSession)
        .where(AdminSession.token == token)
        .values(revoked=True)
    )


async def revoke_all_user_sessions(
    user_id: uuid.UUID, *, session: AsyncSession,
) -> int:
    res = await session.execute(
        update(AdminSession)
        .where(AdminSession.user_id == user_id)
        .where(AdminSession.revoked.is_(False))
        .values(revoked=True)
        .returning(AdminSession.id)
    )
    return len(res.fetchall())


# =====================================================================
# DEPENDENCIES (FastAPI)
# =====================================================================

class _AdminRedirect(HTTPException):
    """Custom Exception, die als 303-Redirect zur Login-Seite gerendert wird."""
    pass


async def require_admin(
    request: Request,
    response: Response,
) -> AdminUser:
    """
    FastAPI-Dependency: laedt Session aus Cookie, validiert, returnet User.

    Bei fehlender / abgelaufener Session: 303 Redirect zu /admin/login.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    async with get_session() as s:
        result = await get_active_session(token, session=s)
        if not result:
            # 303 see-other zu Login (RedirectResponse via Exception-Handler)
            raise _AdminRedirect(
                status_code=303, detail="Login erforderlich",
                headers={"Location": "/admin/login"},
            )
        sess, user = result
        # Session und User in request.state cachen fuer Template-Rendering
        request.state.admin_user = user
        request.state.admin_session = sess
        request.state.admin_csrf = sess.csrf_token
        return user


async def require_csrf(request: Request) -> None:
    """
    Validiert CSRF-Token aus Form-Body gegen Session.

    require_admin() muss ZUERST gelaufen sein.
    """
    sess: Optional[AdminSession] = getattr(request.state, "admin_session", None)
    if not sess:
        raise HTTPException(403, "Keine Session")

    form = await request.form()
    posted = form.get(CSRF_FIELD_NAME)
    if not posted or not secrets.compare_digest(str(posted), sess.csrf_token):
        raise HTTPException(403, "Ungueltiges CSRF-Token")


# =====================================================================
# SETUP-FLOW
# =====================================================================

async def admin_users_exist() -> bool:
    """Wird /admin/setup aufgerufen, ist es nur erlaubt solange das hier False ist."""
    async with get_session() as s:
        result = await s.execute(select(AdminUser).limit(1))
        return result.scalar_one_or_none() is not None


async def create_initial_admin(
    email: str, password: str, *, request: Request,
) -> AdminUser:
    """Legt den ersten Admin-User an. Schlaegt fehl wenn schon einer existiert.

    Hardening (Race-Condition-Fix): zwei parallel eintreffende /admin/setup-
    Requests koennten beide den admin_users_exist()-Check passieren bevor
    einer committed hat → zwei Admins entstehen. Loesung: SERIALIZABLE-
    Transaktion + Re-Check innerhalb derselben Transaktion. Postgres
    serialisiert dann die zweite Anfrage als 409 statt 200.
    """
    if "@" not in email or len(email) > 255:
        raise HTTPException(400, "E-Mail ungueltig")
    if len(password) < 10:
        raise HTTPException(400, "Passwort muss mindestens 10 Zeichen lang sein")

    async with get_session() as s:
        # SERIALIZABLE-Isolation: garantiert dass kein paralleler Setup
        # durchschluepft. Falls zwei Requests parallel SELECT machen
        # bevor einer COMMIT, scheitert der zweite mit
        # SerializationFailure → 500 vom Default-Handler. Beim erneuten
        # Versuch greift dann der admin_users_exist()-Check und es kommt
        # 409. Sicherer als der bisherige reine SELECT-then-INSERT-Pattern.
        await s.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        existing = (await s.execute(
            select(AdminUser).limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(409, "Setup bereits durchgefuehrt")

        user = AdminUser(
            email=email.lower().strip(),
            password_hash=hash_password(password),
            is_active=True,
        )
        s.add(user)
        await s.flush()
        await audit(
            user_id=user.id, action="setup.create_admin",
            target=user.email, request=request, session=s,
        )
        await s.refresh(user)
    return user
