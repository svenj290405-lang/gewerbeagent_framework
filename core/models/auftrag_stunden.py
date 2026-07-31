"""AuftragStunden — geleistete Arbeitsstunden auf einem Auftrag.

Eingetragen wird dort, wo der Handwerker ohnehin seinen Fortschritt
schiebt: unter dem Regler „Arbeit laeuft". Ein Eintrag = ein Mitarbeiter,
ein Tag, eine Stundenzahl, optional wofuer.

**Das ist Auftragszeit, nicht die gesetzliche Arbeitszeiterfassung.**
Der Unterschied ist wichtig, weil er den Zweck festlegt und damit die
Rechtsgrundlage:

    Auftragszeit (hier)     wie lange hat der Auftrag gedauert —
                            Nachkalkulation und Abrechnung
    Arbeitszeiterfassung    Beginn/Ende/Dauer der taeglichen Arbeitszeit
                            nach ArbZG/MiLoG — eigenes Thema, andere
                            Aufbewahrungsfrist, hier NICHT abgebildet

Aus einer Summe „6,5 h auf Auftrag X" laesst sich keine Anwesenheit
ableiten, und sie soll auch nicht dazu benutzt werden. Die Aufschluesselung
je Mitarbeiter steht am Auftrag, damit ein Kollege sieht, was dort schon
geleistet wurde — sie ist bewusst kein betriebsweiter Leistungsvergleich.

Korrekturen: ein Eintrag wird nicht editiert, sondern geloescht und neu
gebucht. Das haelt die Zeile ehrlich (``created_at`` = wann gebucht) und
spart eine Aenderungshistorie, die auf dieser Ebene niemand liest.
"""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base

# Grenzen einer EINZELNEN Buchung. Mehr als 24 h an einem Tag ist immer
# ein Tippfehler (meist eine verrutschte Kommastelle), und 0 h ist keine
# Buchung. Mehrere Eintraege pro Tag sind erlaubt — vormittags und
# nachmittags auf derselben Baustelle ist normal.
STUNDEN_MIN = Decimal("0.25")
STUNDEN_MAX = Decimal("24")
MAX_NOTIZ = 300


class AuftragStunden(Base):
    __tablename__ = "auftrag_stunden"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    angebot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("angebote.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # SET NULL statt CASCADE: ein deaktivierter oder entfernter Mitarbeiter
    # darf die geleisteten Stunden des Auftrags nicht mitnehmen — die
    # Nachkalkulation braucht sie weiterhin. Der Name steht deshalb auch
    # als Schnappschuss daneben.
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    employee_name: Mapped[str] = mapped_column(String(200), nullable=False)

    stunden: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    datum: Mapped[dt.date] = mapped_column(Date, nullable=False)
    notiz: Mapped[str | None] = mapped_column(String(MAX_NOTIZ), nullable=True)

    # created_at + updated_at via Base

    __table_args__ = (
        CheckConstraint(
            "stunden > 0 AND stunden <= 24", name="ck_auftrag_stunden_bereich",
        ),
        Index("ix_auftrag_stunden_angebot_datum", "angebot_id", "datum"),
    )

    def __repr__(self) -> str:
        return (f"<AuftragStunden {self.stunden}h {self.employee_name!r} "
                f"auftrag={self.angebot_id} am={self.datum}>")


__all__ = ["AuftragStunden", "STUNDEN_MIN", "STUNDEN_MAX", "MAX_NOTIZ"]
