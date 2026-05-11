"""Add tenants.data_retention_days

Revision ID: w2yz3la4mb5n
Revises: v1xy2zk3l4m5
Create Date: 2026-05-11 06:30:00.000000

Phase B4: konfigurierbare DSGVO-Retention pro Tenant. Bisher hatten
alle Tenants gleichermassen 14 Tage (Constant in dsgvo_cleanup_cron).
Jetzt kann jeder Tenant eigene Werte zwischen 7 und 365 Tagen haben.
Default 90 — passt zur DSGVO-Konformitaet fuer Stammkunden-
Beziehungen ohne unnoetig lange Speicherung.

Additiv. Bestehende Tenants bekommen automatisch 90.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "w2yz3la4mb5n"
down_revision: Union[str, Sequence[str], None] = "v1xy2zk3l4m5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "data_retention_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("90"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "data_retention_days")
