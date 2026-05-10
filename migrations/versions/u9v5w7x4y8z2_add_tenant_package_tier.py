"""Add tenant.package_tier

Revision ID: u9v5w7x4y8z2
Revises: t8u4v9w2x6y3
Create Date: 2026-05-10 23:30:00.000000

Fuegt die Spalte `tenant.package_tier` hinzu fuer das Paket-System
(Basis/Pro/Enterprise/Custom). Default 'pro' damit bestehende Tenants
beim Backfill ihre Features behalten — `scripts/backfill_tenant_features`
korrigiert nachher zu basis/enterprise/custom wo passend.

Additiv. Kein DROP. Konsistent mit den vorigen Migrationen.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "u9v5w7x4y8z2"
down_revision: Union[str, Sequence[str], None] = "t8u4v9w2x6y3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "package_tier",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pro'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "package_tier")
