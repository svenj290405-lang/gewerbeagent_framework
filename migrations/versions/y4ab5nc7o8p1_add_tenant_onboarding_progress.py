"""Add tenants.onboarding_step + onboarding_completed_at

Revision ID: y4ab5nc7o8p1
Revises: x3za4mb6n7o9
Create Date: 2026-05-12 10:00:00.000000

Onboarding-Tutorial im Telegram-Bot: jeder neue Tenant durchlaeuft
beim ersten /start einen guided Setup-Wizard (Stammdaten, Lexware,
Kalender, etc.). Wir speichern den Fortschritt am Tenant damit der
Wizard bei Unterbrechungen (Restart, /abbrechen) genau dort
weitermacht.

  onboarding_step: int, 0 = noch nicht angefangen, n = bei Schritt n.
  onboarding_completed_at: datetime, NULL = nicht fertig, sonst Timestamp.

Additiv. Bestehende Tenants bekommen step=0 + completed_at=now()
(per Backfill in der app) — sie haben das Setup ueber Admin-UI
gemacht und sollen nicht ploetzlich im Onboarding landen.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "y4ab5nc7o8p1"
down_revision: Union[str, None] = "x3za4mb6n7o9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "onboarding_step",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "onboarding_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Bestehende Tenants als "fertig" markieren — sie kennen das Tool
    # bereits, sollen nicht zwangs-onboarded werden.
    op.execute(
        "UPDATE tenants SET onboarding_completed_at = NOW() "
        "WHERE onboarding_completed_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("tenants", "onboarding_completed_at")
    op.drop_column("tenants", "onboarding_step")
