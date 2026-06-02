"""Rueckruf - strukturierte Rueckrufbitte aus dem Voice-Agent.

Hintergrund: Sipgate unterstuetzt SIP REFER nicht zuverlaessig, eine
Live-Weiterleitung am Telefon ist daher keine Option. Stattdessen nimmt
der Voice-Agent eine strukturierte Rueckrufbitte auf, sobald
  (a) der Anrufer ausdruecklich einen Menschen/Mitarbeiter verlangt oder
      veraergert ist, ODER
  (b) das Anliegen etwas ist, das die KI nicht selbst erledigen kann.

Workflow:
1. Voice-Agent ruft das Tool 'rueckruf_anfordern' auf
   (plugins/voice_init/handler.py -> _handle_rueckruf_anfordern).
2. Hier wird eine Zeile mit Status 'offen' gespeichert.
3. Sofortiger Telegram-Push an den zustaendigen Mitarbeiter/Tenant mit
   Inline-Button '✅ Erledigt'.
4. Handwerker hakt per Button (oder ueber /rueckrufe) ab -> Status
   'erledigt'.
5. /briefing + /rueckrufe zeigen die offenen Rueckrufe.

Bewusst eine eigene Tabelle statt Recycling von Kundengespraech
(Audio-Aufnahmen) oder AnfrageResponse (Web-Formular + Token + Mail):
ein Telefon-Rueckruf hat weder Token noch Pflicht-Mail und soll weder
die Aufnahmen- noch die Formular-Sicht verschmutzen.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# Bearbeitungs-Status (Muster wie FORMULAR_STATUS_* in anfrage.py).
RUECKRUF_STATUS_OFFEN = "offen"
RUECKRUF_STATUS_ERLEDIGT = "erledigt"
RUECKRUF_STATUS_VALID = {RUECKRUF_STATUS_OFFEN, RUECKRUF_STATUS_ERLEDIGT}
RUECKRUF_STATUS_LABEL = {
    RUECKRUF_STATUS_OFFEN: "📞 Offen",
    RUECKRUF_STATUS_ERLEDIGT: "✅ Erledigt",
}


class Rueckruf(Base):
    __tablename__ = "rueckrufe"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # Kundendaten (alle Pflicht ausser E-Mail).
    kunde_name: Mapped[str] = mapped_column(String(300), nullable=False)
    kunde_telefon: Mapped[str] = mapped_column(String(50), nullable=False)
    anliegen: Mapped[str] = mapped_column(Text, nullable=False)
    kunde_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Bearbeitungs-Status. server_default damit Bestands-/Roh-Inserts
    # ohne explizites Setzen sauber 'offen' sind.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False,
        default=RUECKRUF_STATUS_OFFEN, server_default=RUECKRUF_STATUS_OFFEN,
        index=True,
    )

    # Skill-Routing (choose_employee) zur Erfassungszeit. SET NULL damit
    # ein deaktivierter Mitarbeiter die Historie nicht killt.
    assigned_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # Abhaken: wann + von wem.
    erledigt_at: Mapped["dt.datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    erledigt_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )

    # created_at + updated_at aus Base

    def __repr__(self) -> str:
        return (
            f"<Rueckruf id={self.id} kunde={self.kunde_name!r} "
            f"status={self.status}>"
        )
