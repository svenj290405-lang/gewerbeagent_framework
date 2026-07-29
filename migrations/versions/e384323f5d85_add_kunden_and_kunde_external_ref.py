"""Kundendatenbank Phase 1: kunden + kunde_external_ref + kunde_id-FKs

Revision ID: e384323f5d85
Revises: d4f8a1c62b7e
Create Date: 2026-07-16 10:00:00.000000

Legt den Kundenstamm an (kunden — die technische id ist die interne
Kundennummer) und die generische Fremdsystem-Verknuepfung
kunde_external_ref (Lexware zuerst, spaeter weitere Systeme ohne neue
Tabelle). Zusaetzlich bekommt jede kundenbezogene Bestandstabelle ein
nullable kunde_id mit Partial-Index (Muster wie x3za4mb6n7o9).

Rein additiv: kein Code liest oder schreibt die neuen Felder, bis
Phase 2 (Backfill) und Phase 3 (Schreibpfade) deployt sind — siehe
Kundendatenbank_Umsetzungsplan.md. Bestandszeilen bekommen NULL.

Die zwei Unique-Constraints auf kunde_external_ref sind bewusst schon
jetzt dabei (Tabelle ist leer): ein Kunde hat pro System hoechstens
eine ID, und zwei Kunden koennen nie auf denselben Fremdkontakt zeigen.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e384323f5d85"
down_revision: Union[str, None] = "d4f8a1c62b7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Bestandstabellen, die den kunde_id-Verweis bekommen.
KUNDE_ID_TABLES = (
    "angebote",
    "rechnungen",
    "kundengespraeche",
    "rueckrufe",
    "anfrage_tokens",
    "email_conversations",
    "visualisierungen",
    "tenant_kunde_drive",
)


def upgrade() -> None:
    op.create_table(
        "kunden",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("telefon", sa.String(50), nullable=True),
        sa.Column("adresse", sa.String(500), nullable=True),
        sa.Column("identity_key", sa.String(120), nullable=False),
        sa.Column(
            "needs_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "merged_into_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kunden.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id", "identity_key", name="uq_kunden_tenant_identity",
        ),
    )
    op.create_index("ix_kunden_tenant_id", "kunden", ["tenant_id"])

    op.create_table(
        "kunde_external_ref",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kunde_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kunden.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("system", sa.String(50), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id", "kunde_id", "system",
            name="uq_kunde_external_ref_kunde_system",
        ),
        sa.UniqueConstraint(
            "tenant_id", "system", "external_id",
            name="uq_kunde_external_ref_external_id",
        ),
    )
    op.create_index(
        "ix_kunde_external_ref_tenant_id", "kunde_external_ref", ["tenant_id"],
    )
    op.create_index(
        "ix_kunde_external_ref_kunde_id", "kunde_external_ref", ["kunde_id"],
    )

    for table in KUNDE_ID_TABLES:
        op.add_column(
            table,
            sa.Column(
                "kunde_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(
                    "kunden.id",
                    ondelete="SET NULL",
                    name=f"fk_{table}_kunde_id",
                ),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_kunde_id",
            table,
            ["kunde_id"],
            unique=False,
            postgresql_where=sa.text("kunde_id IS NOT NULL"),
        )


def downgrade() -> None:
    for table in reversed(KUNDE_ID_TABLES):
        op.drop_index(f"ix_{table}_kunde_id", table_name=table)
        op.drop_column(table, "kunde_id")
    op.drop_index(
        "ix_kunde_external_ref_kunde_id", table_name="kunde_external_ref",
    )
    op.drop_index(
        "ix_kunde_external_ref_tenant_id", table_name="kunde_external_ref",
    )
    op.drop_table("kunde_external_ref")
    op.drop_index("ix_kunden_tenant_id", table_name="kunden")
    op.drop_table("kunden")
