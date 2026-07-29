"""tenants.brand_color — Primärfarbe der WebApp pro Tenant

Revision ID: a1bd7ef2c904
Revises: z5ac6od8p1q3
Create Date: 2026-06-18 10:00:00.000000

Additiv. NULL = Standard (#0066cc). Inhaber setzt die Farbe über die
Einstellungen in der WebApp. Kein Backfill nötig.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1bd7ef2c904"
down_revision: Union[str, tuple, None] = ("z5ac6od8p1q3", "f3b8d1a2c4e7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("brand_color", sa.String(length=7), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "brand_color")
