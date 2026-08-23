"""cron_heartbeats: Lebenszeichen der Crons dauerhaft ablegen

Revision ID: j2k5n8q1r4t7
Revises: h9k3m6p2r5t8
Create Date: 2026-08-23 22:40:00.000000

Die Heartbeats lagen nur im Speicher des Prozesses. Nach jedem Neustart
sahen deshalb alle Crons kurz tot aus, und jede Pruefung von ausserhalb
des Prozesses meldete "alle Crons tot". Solange der Alarmweg ohnehin
stumm war, fiel das nicht auf — jetzt waere es ein Fehlalarm mit
Zustellung.
"""
from alembic import op
import sqlalchemy as sa


revision = "j2k5n8q1r4t7"
down_revision = "h9k3m6p2r5t8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cron_heartbeats",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("cron_name", sa.String(80), nullable=False, unique=True),
        sa.Column("last_beat", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # Base haengt beide Spalten an JEDES Modell an — fehlen sie hier,
        # scheitert spaeter jeder ORM-Zugriff (siehe geocode_cache).
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cron_heartbeats_cron_name", "cron_heartbeats",
                    ["cron_name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_cron_heartbeats_cron_name", table_name="cron_heartbeats")
    op.drop_table("cron_heartbeats")
