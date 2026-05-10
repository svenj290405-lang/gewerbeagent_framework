"""
EmailConversation = Multi-Turn-Mail-Konversation mit einem Kunden.

Wird vom mail_intake-Plugin gepflegt. Speichert State-Machine + aktuellen
Termin pro (Tenant, Kunden-Mail) so dass das Plugin bei einer Reply
weiss: gibt es schon einen Termin den der Kunde verschieben moechte?

Hard-Delete Cleanup: Konversationen bei denen termin_datum laenger als
14 Tage zurueckliegt werden vom periodischen Cleanup-Job geloescht.
"""
import datetime as dt
import uuid

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


# State-Machine Werte (als String-Konstanten, nicht ENUM, damit
# Migrations einfach bleiben)
STATE_AWAITING_CONFIRMATION = "awaiting_confirmation"
STATE_BOOKED = "booked"
STATE_PROPOSING_SLOTS = "proposing_slots"
STATE_CLOSED = "closed"



# === Klassifikations-Kategorien ===
CLASSIFICATION_RELEVANT_KUNDE = "RELEVANT_KUNDE"
CLASSIFICATION_RELEVANT_GESCHAEFT = "RELEVANT_GESCHAEFT"
CLASSIFICATION_NICHT_RELEVANT = "NICHT_RELEVANT"
CLASSIFICATION_PRIVAT = "PRIVAT"
CLASSIFICATION_UNSICHER = "UNSICHER"

CLASSIFICATIONS_RELEVANT = (CLASSIFICATION_RELEVANT_KUNDE, CLASSIFICATION_RELEVANT_GESCHAEFT)

CONFIDENCE_LOW = "low"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_HIGH = "high"

class EmailConversation(Base):
    """Mail-Konversations-Memory pro (Tenant, Kunden-Mail)."""

    __tablename__ = "email_conversations"

    __table_args__ = (
        Index("ix_email_conv_tenant_kunde", "tenant_id", "kunde_email"),
        Index("ix_email_conv_message_id", "last_message_id"),
        Index("ix_email_conv_termin_datum", "termin_datum"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Welcher Kunde (Mehrmandanten)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Routing-Key: Mail des Kunden (NIE die Tenant-Reply-Adresse)
    kunde_email: Mapped[str] = mapped_column(String(255), nullable=False)
    kunde_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Aktueller Termin (falls schon eingetragen)
    gcal_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    termin_datum: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # Threading: letzte Mail-Message-ID (fuer In-Reply-To-Matching)
    last_message_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_subject: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Konversations-Memory fuer Multi-Turn (Q weiss was er zuletzt fragte)
    last_q_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_user_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # State-Machine
    state: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=STATE_AWAITING_CONFIRMATION,
        server_default=STATE_AWAITING_CONFIRMATION,
        comment="awaiting_confirmation | booked | proposing_slots | closed",
    )

    # Wenn state=proposing_slots: welche Slots wurden vorgeschlagen?
    # Format: [{"datum": "30.04.2026", "uhrzeit": "14:00"}, ...]
    proposed_slots: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    classification: Mapped[str | None] = mapped_column(String(30), nullable=True)
    classification_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    classified_at: Mapped["dt.datetime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    # Phase-4-Multi-Mitarbeiter: welcher Employee bearbeitet diese
    # Konversation? Phase-5-Skill-Router setzt das beim ersten Eingang;
    # Folge-Mails sind sticky (kein Re-Routing). Bei deaktivierten
    # Employees wird der FK auf NULL gesetzt (ON DELETE SET NULL),
    # damit Konversation nicht verloren geht.
    assigned_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # Relationship zum Tenant
    tenant: Mapped["Tenant"] = relationship()  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<EmailConversation {self.kunde_email} @ {self.tenant_id} "
            f"state={self.state} termin={self.termin_datum}>"
        )
