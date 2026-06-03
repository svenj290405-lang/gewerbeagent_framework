"""App-Account-Modelle — Auth + Web-Push für die Inhaber-/Mitarbeiter-PWA.

Die PWA (``/app``) loest den Telegram-Bot als Bedien-Oberflaeche ab. Die
Identitaet ist NICHT ein eigenes Nutzersystem, sondern das bestehende
``Employee`` (is_default==True = Inhaber, sonst Mitarbeiter). Diese Tabellen
haengen daher direkt an ``employees`` — keine parallele User-Tabelle.

Tabellen:
- app_sessions        Server-side Web-Sessions (Cookie traegt nur den Token),
                      analog ``admin_sessions``, aber an einen Employee gebunden.
- app_login_tokens    Einmalige Magic-Link-Token fuer den passwortlosen Login
                      (Link per Mail). Baut auf demselben Muster wie die
                      Employee-Aktivierungs-Token auf.
- push_subscriptions  Web-Push-Abos (VAPID). Der Push-Payload ist bewusst
                      inhaltslos/minimal — FCM/APNs sehen keine PII; Inhalte
                      laedt die App nach Login vom EU-Server.

Tenant-Isolation: ``tenant_id`` ist auf allen drei Tabellen denormalisiert
mitgefuehrt, damit App-Queries ohne Join hart auf den eigenen Tenant
gescoped werden koennen.
"""
from __future__ import annotations

import datetime as dt
import secrets
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


def _new_token() -> str:
    """Opaker Token (Session-Cookie, Magic-Link, CSRF). Nur in der DB."""
    return secrets.token_urlsafe(40)


# Lebensdauern (an einer Stelle, damit app_auth.py + Migration konsistent sind)
APP_SESSION_LIFETIME = dt.timedelta(days=30)
APP_LOGIN_TOKEN_LIFETIME = dt.timedelta(minutes=20)


class AppSession(Base):
    """Server-side Web-Session der PWA — Cookie traegt nur den Token.

    Analog ``AdminSession``, aber gebunden an einen ``Employee`` (nicht an
    einen Admin-User). 30 Tage Lebensdauer (App soll wie eine native App
    eingeloggt bleiben), Sliding-Window-Bump in app_auth.
    """

    __tablename__ = "app_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    token: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True, default=_new_token,
    )
    csrf_token: Mapped[str] = mapped_column(
        String(64), nullable=False, default=_new_token,
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AppLoginToken(Base):
    """Einmaliger Magic-Link-Token fuer passwortlosen Login.

    Flow: Employee gibt Mail ein -> Token erzeugt + Link per Mail -> Klick
    auf ``/app/login/<token>`` loest den Token ein (atomar, einmalig) und
    legt eine ``AppSession`` an. ``used_at`` markiert Verbrauch; abgelaufene
    oder verbrauchte Token werden abgewiesen.
    """

    __tablename__ = "app_login_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    token: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True, default=_new_token,
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    used_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PushSubscription(Base):
    """Web-Push-Abo (VAPID) eines App-Geraets.

    Eine Zeile pro Browser/Geraet. ``endpoint`` ist die vom Push-Service
    (FCM/APNs/Mozilla) vergebene URL und global eindeutig. ``p256dh``+``auth``
    sind die oeffentlichen Client-Schluessel fuer die Payload-Verschluesselung
    — sie enthalten KEINE PII und liegen daher im Klartext.
    """

    __tablename__ = "push_subscriptions"

    __table_args__ = (
        Index("ix_push_sub_employee", "employee_id"),
        Index("ix_push_sub_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    endpoint: Mapped[str] = mapped_column(
        String(2048), unique=True, nullable=False,
    )
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)


__all__ = [
    "AppSession",
    "AppLoginToken",
    "PushSubscription",
    "APP_SESSION_LIFETIME",
    "APP_LOGIN_TOKEN_LIFETIME",
]
