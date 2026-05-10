"""Add tenant_kunde_drive Tabelle

Revision ID: t8u4v9w2x6y3
Revises: s7t9u3v5w8x2
Create Date: 2026-05-10 23:00:00.000000

Mapping (Tenant, Kunde) -> Google-Drive-Ordner-ID.
Pro Kunde wird beim ersten /archiv <name>-Upload lazy ein Ordner im
Drive des Tenants erstellt. Diese Tabelle merkt sich den Lookup damit
Folge-Uploads in den gleichen Ordner gehen.

Additiv. Kein DROP.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "t8u4v9w2x6y3"
down_revision: Union[str, Sequence[str], None] = "s7t9u3v5w8x2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_kunde_drive",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kunde_key", sa.String(120), nullable=False),
        sa.Column("kunde_name", sa.String(200), nullable=False),
        sa.Column("drive_folder_id", sa.String(100), nullable=False),
        sa.Column("drive_folder_url", sa.String(500), nullable=False),
        sa.Column("upload_count", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("last_upload_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "kunde_key",
                            name="uq_tenant_kunde_drive_key"),
    )
    op.create_index("ix_tenant_kunde_drive_tenant_id",
                    "tenant_kunde_drive", ["tenant_id"])
    op.create_index("ix_tenant_kunde_drive_last_upload",
                    "tenant_kunde_drive", ["tenant_id", "last_upload_at"])


def downgrade() -> None:
    op.drop_index("ix_tenant_kunde_drive_last_upload",
                  table_name="tenant_kunde_drive")
    op.drop_index("ix_tenant_kunde_drive_tenant_id",
                  table_name="tenant_kunde_drive")
    op.drop_table("tenant_kunde_drive")
