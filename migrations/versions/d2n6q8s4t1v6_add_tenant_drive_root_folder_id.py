"""tenants.drive_root_folder_id

Revision ID: d2n6q8s4t1v6
Revises: c1m5p7r3t8u9
Create Date: 2026-05-17 19:55:00.000000

Cached Drive-Root-Folder-ID pro Tenant.

Vorher: _ensure_root_folder hat den Root-Ordner bei JEDEM Upload
ueber name='Gewerbeagent — <company>' gesucht und neu angelegt wenn
nicht gefunden. Effekt: bei Tenant-Umbenennung oder Naming-Konvention-
Wechsel im Code entstand jedes Mal ein NEUER, leerer Root-Ordner —
alte Dateien wurden zu Waisen.

Jetzt: einmal gefunden/angelegt, in dieser Spalte gecacht, ab dann
ueber files.get(folder_id) gefunden. Naming-Drift unmoeglich.

Bestehende Tenants: drive_root_folder_id bleibt NULL — beim naechsten
Upload greift der Search-Fallback in _ensure_root_folder und schreibt
die ID dann ein.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2n6q8s4t1v6"
down_revision: Union[str, None] = "c1m5p7r3t8u9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "drive_root_folder_id",
            sa.String(length=200),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "drive_root_folder_id")
