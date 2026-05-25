"""health_check_results — Persistenz fuer den taeglichen System-Health-Check

Revision ID: t6h9k2m5p8r1
Revises: s5n9j4l7q3r8
Create Date: 2026-05-25 11:00:00.000000

Speichert das Ergebnis des Daily-Health-Checks (DB / Telegram-Bot / Crons),
damit es im Admin-Tool (/admin/health) angezeigt werden kann und ueber
Container-Restarts hinweg erhalten bleibt (anders als der In-Memory-
Heartbeat in core/integrations/cron_health.py).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "t6h9k2m5p8r1"
down_revision = "s5n9j4l7q3r8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "health_check_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("db_ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("telegram_ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("crons_ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("alert_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_health_check_results_checked_at",
        "health_check_results", ["checked_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_health_check_results_checked_at",
        table_name="health_check_results",
    )
    op.drop_table("health_check_results")
