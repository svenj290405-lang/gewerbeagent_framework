"""KundeExternalRef — generische Verknuepfung Kunde ↔ Fremdsystem-ID.

Haelt pro Kunde die ID, unter der er in einem Fremdsystem gefuehrt
wird (Lexware-Kontakt zuerst, spaeter z. B. Drive). Eine generische
Tabelle statt einer Spalte pro System, damit weitere Systeme ohne
Migration dazukommen koennen.

Zwei Unique-Constraints tragen die Fachlichkeit:
`(tenant_id, kunde_id, system)` — ein Kunde hat pro System hoechstens
eine ID. `(tenant_id, system, external_id)` — zwei Kunden koennen nie
auf denselben Fremdkontakt zeigen; ohne diesen waere die Vermischung,
die wir intern beseitigen, auf Lexware-Seite weiter moeglich.

`external_id` ist bewusst String: Lexware nutzt UUIDs (abgelegt als
str(uuid), lowercase mit Bindestrichen), andere Systeme womoeglich
andere Formate.
"""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base

# Bekannte Systeme — als Konstante, damit Tippfehler nicht erst am
# Unique-Constraint auffallen.
REF_SYSTEM_LEXWARE = "lexware"


class KundeExternalRef(Base):
    """Fremdsystem-ID eines Kunden (z. B. Lexware-Kontakt-ID)."""
    __tablename__ = "kunde_external_ref"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kunde_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kunden.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    system: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # created_at + updated_at via Base

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "kunde_id", "system",
            name="uq_kunde_external_ref_kunde_system",
        ),
        UniqueConstraint(
            "tenant_id", "system", "external_id",
            name="uq_kunde_external_ref_external_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<KundeExternalRef kunde={self.kunde_id} "
            f"{self.system}={self.external_id!r}>"
        )


__all__ = ["KundeExternalRef", "REF_SYSTEM_LEXWARE"]
