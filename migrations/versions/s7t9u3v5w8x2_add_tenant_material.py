"""Add tenant_material + material_bestellung tables

Revision ID: s7t9u3v5w8x2
Revises: r4m8h3k6n2p7
Create Date: 2026-05-10 22:00:00.000000

Erstellt:
- tenant_material           Stammdaten der nachbestellbaren Artikel
- material_bestellung       Audit-Log jeder ausgeloesten Bestellung

Alle Tabellen additiv. Kein DROP.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "s7t9u3v5w8x2"
down_revision: Union[str, Sequence[str], None] = "r4m8h3k6n2p7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- tenant_material ----------
    op.create_table(
        "tenant_material",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("bestell_link", sa.String(2000), nullable=False),
        sa.Column("lieferant_name", sa.String(200), nullable=True),
        sa.Column("einheit", sa.String(30), nullable=False,
                  server_default=sa.text("'Stück'")),
        sa.Column("standard_menge", sa.Integer(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("aktiv", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "slug",
                            name="uq_tenant_material_slug"),
    )
    op.create_index("ix_tenant_material_tenant_id", "tenant_material",
                    ["tenant_id"])
    op.create_index("ix_tenant_material_active", "tenant_material",
                    ["tenant_id", "aktiv"])

    # ---------- material_bestellung ----------
    op.create_table(
        "material_bestellung",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("material_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant_material.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("employees.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("material_name", sa.String(200), nullable=False),
        sa.Column("bestell_link", sa.String(2000), nullable=False),
        sa.Column("menge", sa.Integer(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("einheit", sa.String(30), nullable=False,
                  server_default=sa.text("'Stück'")),
        sa.Column("bestell_art", sa.String(20), nullable=False,
                  server_default=sa.text("'link'")),
        sa.Column("metadata", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_material_bestellung_tenant_id",
                    "material_bestellung", ["tenant_id"])
    op.create_index("ix_material_bestellung_tenant_time",
                    "material_bestellung", ["tenant_id", "created_at"])
    op.create_index("ix_material_bestellung_material",
                    "material_bestellung", ["material_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_material_bestellung_material",
                  table_name="material_bestellung")
    op.drop_index("ix_material_bestellung_tenant_time",
                  table_name="material_bestellung")
    op.drop_index("ix_material_bestellung_tenant_id",
                  table_name="material_bestellung")
    op.drop_table("material_bestellung")

    op.drop_index("ix_tenant_material_active", table_name="tenant_material")
    op.drop_index("ix_tenant_material_tenant_id", table_name="tenant_material")
    op.drop_table("tenant_material")
