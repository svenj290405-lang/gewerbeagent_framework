"""anfrage_tokens.kunde_telefon + Index fuer Voice-Session-Lookup

Revision ID: a9k2m4n6p8q1
Revises: 8b2e4f6a7d5c
Create Date: 2026-05-17 19:00:00.000000

Voice-Flow Source-of-Truth fuer kunde_email-Resolution:
- _handle_save_contact erzeugt AnfrageToken (Mail+Name+Anliegen).
  Wir speichern ab jetzt zusaetzlich die normalisierte Telefon-Nummer
  des Anrufers.
- _handle_buche_termin lookt vor der Kalender-Buchung den juengsten
  Token derselben Nummer im selben Tenant, zieht kunde_email raus und
  speichert sie als extendedProperty am Calendar-Event.

Damit findet find_events bei spaeterem Storno-Anruf den Termin auch
wenn der Kunde nicht mehr seine Nummer kennt sondern nur seine Mail.

Schema:
- kunde_telefon: nullable String (50) — gespeichert in normalisierter
  Form (Ziffern-only, ohne fuehrende 0 nach 49). Mail-Caller setzen
  NULL (Telefon kennt die Mail-Pipeline meist nicht).
- Index (tenant_id, kunde_telefon): partial WHERE NOT NULL spart Platz
  weil Mail-erzeugte Tokens kein Telefon haben; Postgres unterstuetzt
  das nativ via WHERE-Klausel im CREATE INDEX.

Additiv. Bestehende Zeilen bleiben mit NULL — Lookup gibt fuer alte
Tokens dann einfach None zurueck (Caller-Fallback greift).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9k2m4n6p8q1"
down_revision: Union[str, None] = "8b2e4f6a7d5c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "anfrage_tokens",
        sa.Column("kunde_telefon", sa.String(length=50), nullable=True),
    )
    # Partial Index: nur Zeilen mit Telefon sind fuer den Voice-Lookup
    # interessant. Spart Index-Groesse bei mailgetriebenen Bestands-
    # Tokens.
    op.create_index(
        "ix_anfrage_tokens_tenant_phone",
        "anfrage_tokens",
        ["tenant_id", "kunde_telefon"],
        unique=False,
        postgresql_where=sa.text("kunde_telefon IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_anfrage_tokens_tenant_phone", table_name="anfrage_tokens")
    op.drop_column("anfrage_tokens", "kunde_telefon")
