"""tenant_kunde_drive: kunde_email + kunde_telefon (Identitaets-Referenz)

Der Kunden-Ordner wird ab jetzt ueber E-Mail/Telefon identifiziert
(kunde_key = "email:..." / "tel:..." / slug-Fallback), damit zwei Kunden
mit gleichem Namen nicht denselben Ordner teilen. Die beiden neuen
Spalten speichern die Quelle des Keys zur Nachvollziehbarkeit.

Nullable — Bestands-Zeilen behalten ihren Namens-Slug-Key und werden
beim naechsten Zugriff mit Mail/Telefon lazy umgeschluesselt (Adoption).

Revision ID: f3a7c1e9b2d4
Revises: d1e5f9a2c6b4
Create Date: 2026-05-20 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3a7c1e9b2d4"
down_revision: Union[str, Sequence[str], None] = "d1e5f9a2c6b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenant_kunde_drive",
        sa.Column("kunde_email", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "tenant_kunde_drive",
        sa.Column("kunde_telefon", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_kunde_drive", "kunde_telefon")
    op.drop_column("tenant_kunde_drive", "kunde_email")
