"""OAuth-State-Modell fuer persistente State-Speicherung."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    # Phase 1 Multi-OAuth: optional welcher Mitarbeiter den Flow startet
    # (NULL = Tenant-Owner / Default-Employee).
    employee_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Welches Scope-Profil dieser Flow angefordert hat: "voll" (Inhaber)
    # oder "mitarbeiter" (nur Verfuegbarkeit + App-Kalender). MUSS
    # mitreisen — die Scope-Liste wird beim Bauen der Auth-URL UND beim
    # Token-Tausch gebraucht, sonst wirft oauthlib "Scope has changed".
    # NULL = voll (Bestands-States und der Inhaber-Pfad).
    scope_profil: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
