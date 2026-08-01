"""automation_settings-Tabelle: Automatisierungsgrad pro Tenant und Funktion

Revision ID: f4b7d2e9a1c8
Revises: e2c7a4d1b596
Create Date: 2026-08-01 12:00:00.000000

Einstellungen → Automatisierung: der Betrieb stellt pro Funktion ein, wie
selbstaendig Q handeln darf (manuell | assistiert | automatisch). Die
Registry der Funktionen liegt in core/features/automations.py.

Bewusst KEIN Backfill: fehlt die Zeile, gilt Automation.default_mode aus
der Registry. Die Defaults sind genau das Verhalten von VOR dieser
Migration (Chat-Aktionen 'assistiert' = Bestaetigung einholen, Telefon
und Mail-Auto-Antwort 'automatisch' = wie bisher). Fuer bestehende
Betriebe aendert sich durch das Deployment also nichts, und neue
Automatisierungen in der Registry brauchen keine Datenwanderung.

Der CheckConstraint spiegelt ALL_MODES aus der Registry — beides zusammen
aendern, sonst schlaegt der Import-Assert in core/models/automation_setting.py
fehl.

Additiv, nur eine neue Tabelle.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4b7d2e9a1c8"
down_revision: Union[str, None] = "e2c7a4d1b596"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "automation_settings",
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
        sa.Column("automation_key", sa.String(length=50), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        # Base setzt created_at/updated_at bei jedem Modell — die Migration
        # muss sie mitbringen, sonst kippt das erste INSERT.
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
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id", "automation_key", name="uq_tenant_automation"
        ),
        sa.CheckConstraint(
            "mode IN ('manuell','assistiert','automatisch')",
            name="ck_automation_mode",
        ),
    )
    # Lesepfad ist immer "alle Einstellungen eines Tenants" (ein Query pro
    # Chat-Befehl, danach 60s Cache) — deshalb Index auf tenant_id.
    op.create_index(
        "ix_automation_settings_tenant_id",
        "automation_settings",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_settings_tenant_id", table_name="automation_settings"
    )
    op.drop_table("automation_settings")
