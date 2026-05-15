"""EmployeeActivationToken — One-Time-Use-Token fuer Mitarbeiter-Onboarding.

Lifecycle:
1. Inhaber legt Mitarbeiter via `/mitarbeiter neu` an.
2. Direkt nach Employee-Insert wird ein Token erzeugt
   (`create_activation_token`) und der Inhaber bekommt einen Deep-Link
   `https://t.me/{bot_username}?start=activate_{token}` per Telegram.
3. Mitarbeiter klickt den Link, /start-Handler ruft
   `consume_activation_token` → setzt `employee.telegram_chat_id` und
   markiert den Token mit `used_at`.
4. Token kann nicht erneut eingeloest werden (one-time-use).

Gueltigkeit: 7 Tage ab `created_at`. Abgelaufene Tokens bleiben in der
Tabelle (Audit), `consume_activation_token` lehnt sie ab.
"""
from __future__ import annotations

import datetime as dt
import secrets
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


# Token-Laenge in Bytes (token_urlsafe liefert ca. 4/3 davon als String —
# 48 Bytes → ~64 Zeichen, passt in String(64).)
_TOKEN_BYTES = 48
DEFAULT_TTL_DAYS = 7


class EmployeeActivationToken(Base):
    """Ein einmal verwendbares Aktivierungs-Token fuer einen Mitarbeiter."""

    __tablename__ = "employee_activation_tokens"

    __table_args__ = (
        Index("ix_activation_employee", "employee_id"),
        Index("ix_activation_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    used_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    employee = relationship("Employee", lazy="joined")

    def is_valid(self, *, now: dt.datetime | None = None) -> bool:
        """True wenn weder benutzt noch abgelaufen."""
        now = now or dt.datetime.now(dt.timezone.utc)
        return self.used_at is None and self.expires_at > now


def _generate_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


async def create_activation_token(
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> EmployeeActivationToken:
    """Erzeugt einen neuen Aktivierungs-Token fuer einen Mitarbeiter.

    Mehrere offene Tokens pro Mitarbeiter sind erlaubt — der Inhaber
    kann den Link erneut anfordern wenn der erste verloren geht.
    `consume_activation_token` nimmt den ersten gueltigen.
    """
    from core.database import AsyncSessionLocal
    now = dt.datetime.now(dt.timezone.utc)
    obj = EmployeeActivationToken(
        tenant_id=tenant_id,
        employee_id=employee_id,
        token=_generate_token(),
        expires_at=now + dt.timedelta(days=ttl_days),
    )
    async with AsyncSessionLocal() as session:
        session.add(obj)
        await session.commit()
        await session.refresh(obj)
        session.expunge(obj)
    return obj


async def consume_activation_token(
    token_str: str,
    *,
    now: dt.datetime | None = None,
) -> EmployeeActivationToken | None:
    """Validiert + markiert einen Token als benutzt — atomar.

    Returns:
        EmployeeActivationToken bei Erfolg (used_at gesetzt),
        None wenn Token nicht existiert, abgelaufen oder bereits benutzt.

    Caller kann aus dem Return-Wert `employee_id` lesen und die
    Telegram-Chat-ID am Mitarbeiter setzen.
    """
    from core.database import AsyncSessionLocal
    now = now or dt.datetime.now(dt.timezone.utc)
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(EmployeeActivationToken)
            .where(EmployeeActivationToken.token == token_str)
        )).scalar_one_or_none()
        if row is None or not row.is_valid(now=now):
            return None
        row.used_at = now
        await session.commit()
        await session.refresh(row)
        session.expunge(row)
        return row
