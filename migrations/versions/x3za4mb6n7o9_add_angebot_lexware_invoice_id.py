"""Add angebote.lexware_invoice_id

Revision ID: x3za4mb6n7o9
Revises: w2yz3la4mb5n
Create Date: 2026-05-12 08:00:00.000000

Auftrags-Lifecycle: nach /angebot wird im Pipeline-Confirm-Schritt ein
Lexware-Invoice-Draft mit den gleichen Positionen vorbereitet. Damit wir
diesen Draft spaeter (bei Status 'arbeit_fertig' im /auftraege-Flow)
nochmal anfassen koennen — finalisieren, PDF holen, Mail an Kunde — muss
die Lexware-Invoice-ID am Angebot persistiert sein.

Additiv. Bestehende Angebote bekommen NULL.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "x3za4mb6n7o9"
down_revision: Union[str, None] = "w2yz3la4mb5n"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "angebote",
        sa.Column(
            "lexware_invoice_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    # Optional: Index — wir suchen kuenftig nach lexware_invoice_id wenn
    # ein Mail-Reply auf eine Rechnung kommt (analog zu lexware_quotation_id).
    op.create_index(
        "ix_angebote_lexware_invoice_id",
        "angebote",
        ["lexware_invoice_id"],
        unique=False,
        postgresql_where=sa.text("lexware_invoice_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_angebote_lexware_invoice_id",
        table_name="angebote",
    )
    op.drop_column("angebote", "lexware_invoice_id")
