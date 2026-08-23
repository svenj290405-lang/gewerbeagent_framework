"""
TenantKnowledge = Wissensbasis-Snippet pro Tenant.

Wird in der App gepflegt (Mehr → Wissen). Der Handwerker traegt strukturiert
Infos ein (Materialien, Preise, Anfahrt, Notfall, Oeffnungszeiten, FAQ).
Voice- und Mail-Plugin lesen die passenden Eintraege und geben sie als Kontext
an den KI-Agenten.

Struktur bewusst flach: ein Eintrag = eine Kategorie + Freitext.
Keine Vektor-Suche, kein Embedding - reicht fuer 5-30 Snippets pro Tenant;
ab ~25 Eintraegen waehlt core/services/wissen.py per Trigramm-Aehnlichkeit
aus, statt alles in den Prompt zu kippen.

Metadaten (seit e6x3z9m5p2s7):
  quelle       - woher der Eintrag kommt (mensch/q/template/import)
  sichtbarkeit - kunde = darf Q am Telefon/per Mail sagen;
                 intern = nur der Betrieb sieht es (Einkaufspreise,
                 Margen). WICHTIG: der Voice-Block geht 1:1 an
                 ElevenLabs — interne Eintraege duerfen da nie rein.
  aktiv        - stilllegen statt loeschen
  zuletzt_bestaetigt_am - Basis fuer den Frische-Ping
"""
import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


# Erlaubte Kategorien (lose, kein DB-Constraint - nur Doku)
KATEGORIE_LEISTUNGEN = "leistungen"
KATEGORIE_MATERIALIEN = "materialien"
KATEGORIE_PREISE = "preise"
KATEGORIE_ANFAHRT = "anfahrt"
KATEGORIE_OEFFNUNGSZEITEN = "oeffnungszeiten"
KATEGORIE_NOTFALL = "notfall"
KATEGORIE_BESONDERHEITEN = "besonderheiten"
KATEGORIE_FAQ = "faq"

ALLE_KATEGORIEN = (
    KATEGORIE_LEISTUNGEN,
    KATEGORIE_MATERIALIEN,
    KATEGORIE_PREISE,
    KATEGORIE_ANFAHRT,
    KATEGORIE_OEFFNUNGSZEITEN,
    KATEGORIE_NOTFALL,
    KATEGORIE_BESONDERHEITEN,
    KATEGORIE_FAQ,
)

# Display-Labels fuer den User (Reihenfolge = ALLE_KATEGORIEN)
KATEGORIE_LABELS = {
    KATEGORIE_LEISTUNGEN: "Leistungen / Gewerke",
    KATEGORIE_MATERIALIEN: "Materialien",
    KATEGORIE_PREISE: "Preise & Stundensatz",
    KATEGORIE_ANFAHRT: "Anfahrt & Einzugsgebiet",
    KATEGORIE_OEFFNUNGSZEITEN: "Oeffnungszeiten",
    KATEGORIE_NOTFALL: "Notfall-Logik",
    KATEGORIE_BESONDERHEITEN: "Besonderheiten / Garantie / Zertifikate",
    KATEGORIE_FAQ: "FAQ / Sonstiges",
}


# --- Herkunft eines Eintrags -----------------------------------------------
QUELLE_MENSCH = "mensch"      # in der App getippt
QUELLE_Q = "q"                # Q hat es sich auf Zuruf gemerkt
QUELLE_TEMPLATE = "template"  # Branchen-Vorlage beim Onboarding
QUELLE_IMPORT = "import"      # aus Website/PDF importiert und bestaetigt
ALLE_QUELLEN = (QUELLE_MENSCH, QUELLE_Q, QUELLE_TEMPLATE, QUELLE_IMPORT)

QUELLE_LABELS = {
    QUELLE_MENSCH: "selbst eingetragen",
    QUELLE_Q: "von Q gemerkt",
    QUELLE_TEMPLATE: "Branchen-Vorlage",
    QUELLE_IMPORT: "importiert",
}

# --- Sichtbarkeit ----------------------------------------------------------
# Die Grenze zwischen "darf der Kunde hoeren" und "nur intern". Alles was
# SICHTBARKEIT_KUNDE ist, landet im Voice-System-Prompt (ElevenLabs) und in
# Kundenmails — hier wird also mit entschieden, was den Betrieb verlaesst.
SICHTBARKEIT_KUNDE = "kunde"
SICHTBARKEIT_INTERN = "intern"
ALLE_SICHTBARKEITEN = (SICHTBARKEIT_KUNDE, SICHTBARKEIT_INTERN)

SICHTBARKEIT_LABELS = {
    SICHTBARKEIT_KUNDE: "Q darf es Kunden sagen",
    SICHTBARKEIT_INTERN: "nur intern",
}

# Kategorien, die typischerweise veralten — nur die mahnt der Frische-Ping an.
FRISCHE_KATEGORIEN = (KATEGORIE_PREISE, KATEGORIE_MATERIALIEN)

# Ab wann ein Preis-Eintrag als moeglicherweise veraltet gilt.
FRISCHE_TAGE = 365


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

    # Herkunft — eine der ALLE_QUELLEN
    quelle: Mapped[str] = mapped_column(
        String(20), nullable=False, default=QUELLE_MENSCH,
        server_default=QUELLE_MENSCH,
    )

    # kunde | intern — steuert, ob der Eintrag nach draussen darf
    sichtbarkeit: Mapped[str] = mapped_column(
        String(10), nullable=False, default=SICHTBARKEIT_KUNDE,
        server_default=SICHTBARKEIT_KUNDE,
    )

    # Stillgelegte Eintraege bleiben sichtbar, gehen aber in keinen Prompt
    aktiv: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Wann der Inhaber zuletzt "ja, gilt noch" gesagt hat (Frische-Ping)
    zuletzt_bestaetigt_am: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
            f"{self.sichtbarkeit} len={len(self.text)}>"
        )
