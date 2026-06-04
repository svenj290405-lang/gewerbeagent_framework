"""app-usage-events: Aktivitaets-Tracking der PWA

Legt die Tabelle ``app_usage_events`` an (siehe
core/models/app_usage_event.py): eine Zeile pro Nutzer-Aktion
(Login, Assistent-Befehl, Assistent-Aktion, Diktat), mit employee_id-Bezug
fuer die Pro-Mitarbeiter-Aktivitaet.

Erbt created_at/updated_at von Base (hier explizit aufgefuehrt, Base-Falle).

Revision ID: d7f3a1c9e6b4
Revises: c4e8a2b6f1d9
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "d7f3a1c9e6b4"
down_revision: Union[str, Sequence[str], None] = "c4e8a2b6f1d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_usage_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "employee_id", UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_app_usage_tenant_kind_created", "app_usage_events",
        ["tenant_id", "kind", "created_at"],
    )
    op.create_index(
        "ix_app_usage_tenant_emp", "app_usage_events",
        ["tenant_id", "employee_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_app_usage_tenant_emp", table_name="app_usage_events")
    op.drop_index("ix_app_usage_tenant_kind_created", table_name="app_usage_events")
    op.drop_table("app_usage_events")
