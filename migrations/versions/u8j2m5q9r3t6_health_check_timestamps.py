"""health_check_results: created_at/updated_at nachruesten (Base-Spalten)

Revision ID: u8j2m5q9r3t6
Revises: t6h9k2m5p8r1
Create Date: 2026-05-25 11:20:00.000000

Die Tabellen-Migration t6h9k2m5p8r1 hatte die von core.database.base.Base
automatisch erwarteten Audit-Spalten created_at/updated_at vergessen —
dadurch schlug jeder INSERT mit UndefinedColumnError ("created_at does not
exist") fehl. Hier nachgeruestet (Tabelle war leer, daher unproblematisch).
"""
from alembic import op
import sqlalchemy as sa


revision = "u8j2m5q9r3t6"
down_revision = "t6h9k2m5p8r1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "health_check_results",
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.add_column(
        "health_check_results",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("health_check_results", "updated_at")
    op.drop_column("health_check_results", "created_at")
