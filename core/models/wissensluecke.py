"""Wissensluecke = eine Kundenfrage, die die Wissensbasis nicht beantworten konnte.

Warum es das gibt: Die Wissensbasis wuchs bisher nur, wenn der Inhaber von
sich aus daran dachte etwas einzutragen. Gleichzeitig lief taeglich der
beste denkbare Bedarfs-Indikator ungenutzt durch — die Fragen, die echte
Kunden am Telefon oder per Mail stellen und auf die Q nichts fand. Die
landeten ausschliesslich als ``treffer=0`` im Logfile.

Jetzt werden sie hier persistiert, in "Aktuelles" als kleine Aufgabe
angezeigt ("3 Fragen konnte ich diese Woche nicht beantworten"), und die
Antwort des Inhabers wird direkt zu einem ``TenantKnowledge``-Eintrag.
Damit pflegt sich die Wissensbasis aus dem laufenden Betrieb.

Dedup: dieselbe Frage zaehlt hoch (``anzahl``) statt eine neue Zeile zu
erzeugen — sonst ersaeuft die Liste bei einer haeufigen Frage.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# Ueber welchen Kanal die Frage reinkam
KANAL_VOICE = "voice"
KANAL_MAIL = "mail"
KANAL_APP = "app"
ALLE_KANAELE = (KANAL_VOICE, KANAL_MAIL, KANAL_APP)

KANAL_LABELS = {
    KANAL_VOICE: "Anruf",
    KANAL_MAIL: "Mail",
    KANAL_APP: "App",
}

# Bearbeitungsstand
STATUS_OFFEN = "offen"
STATUS_BEANTWORTET = "beantwortet"   # wurde zu einem Wissens-Eintrag
STATUS_VERWORFEN = "verworfen"       # unwichtig / Einzelfall
ALLE_STATUS = (STATUS_OFFEN, STATUS_BEANTWORTET, STATUS_VERWORFEN)


class Wissensluecke(Base):
    """Eine unbeantwortete Frage aus dem laufenden Betrieb."""

    __tablename__ = "wissensluecken"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Die Frage so, wie sie gestellt wurde (Voice: das Tool-Argument,
    # Mail: die Kundenfrage). Fremdtext — nie als Anweisung behandeln.
    frage: Mapped[str] = mapped_column(String(500), nullable=False)

    kanal: Mapped[str] = mapped_column(String(20), nullable=False)

    # Wer gefragt hat, soweit bekannt (Mail-Name/Anrufer). Optional.
    kunde: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=STATUS_OFFEN,
        server_default=STATUS_OFFEN,
    )

    # Wie oft dieselbe Frage kam — sortiert die Liste nach Dringlichkeit
    anzahl: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    zuletzt_gefragt_am: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )

    erledigt_am: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Der Wissens-Eintrag, der aus dieser Luecke entstanden ist. Wird der
    # spaeter geloescht, bleibt die Luecke als Historie stehen (SET NULL).
    knowledge_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_knowledge.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_wissensluecken_tenant_status", "tenant_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<Wissensluecke {self.status} {self.kanal} x{self.anzahl} "
            f"{self.frage[:40]!r}>"
        )
