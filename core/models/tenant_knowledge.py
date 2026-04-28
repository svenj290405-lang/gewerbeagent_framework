"""
TenantKnowledge = Wissensbasis-Snippet pro Tenant.

Wird vom Telegram-Bot gepflegt. Handwerker schickt /wissen, traegt strukturiert
Infos ein (Materialien, Preise, Anfahrt, Notfall, Oeffnungszeiten, FAQ).
Voice- und Mail-Plugin lesen die passenden Eintraege und geben sie als Kontext
an den KI-Agenten.

Struktur bewusst flach: ein Eintrag = eine Kategorie + Freitext.
Keine Vektor-Suche, kein Embedding - reicht fuer 5-30 Snippets pro Tenant.
"""
import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


# Erlaubte Kategorien (lose, kein DB-Constraint - nur Doku)
KATEGORIE_MATERIALIEN = "materialien"
KATEGORIE_PREISE = "preise"
KATEGORIE_ANFAHRT = "anfahrt"
KATEGORIE_NOTFALL = "notfall"
KATEGORIE_OEFFNUNGSZEITEN = "oeffnungszeiten"
KATEGORIE_FAQ = "faq"

ALLE_KATEGORIEN = (
    KATEGORIE_MATERIALIEN,
    KATEGORIE_PREISE,
    KATEGORIE_ANFAHRT,
    KATEGORIE_NOTFALL,
    KATEGORIE_OEFFNUNGSZEITEN,
    KATEGORIE_FAQ,
)


class TenantKnowledge(Base):
    """Ein Wissens-Snippet eines Tenants, kategorisiert."""

    __tablename__ = "tenant_knowledge"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Eine der ALLE_KATEGORIEN-Konstanten
    kategorie: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Freitext, max 2000 Zeichen
    text: Mapped[str] = mapped_column(String(2000), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    tenant: Mapped["Tenant"] = relationship()  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<TenantKnowledge {self.kategorie} @ {self.tenant_id} "
            f"len={len(self.text)}>"
        )
