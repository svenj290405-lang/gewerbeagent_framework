"""Telegram-Ausbau: Tabellen, Spalten und Bot-Konfiguration entfernen

Revision ID: d5w2y8k4n6q1
Revises: c3t9r5p2m8q4
Create Date: 2026-08-21 08:30:00.000000

Der Telegram-Bot ist am 2026-08-21 aus DSGVO-Gruenden komplett entfernt
worden (Art. 44 ff. — Telegram FZ-LLC sitzt in Dubai, es gab keinen
tragfaehigen Uebermittlungspfad). Diese Migration raeumt hinterher:

Personenbezogene Daten (das eigentliche Ziel):
  - employees.telegram_chat_id  — Chat-IDs sind Personenbezug
  - tenants.telegram_chat_id    — dito (Legacy-Spiegel)
  - telegram_state              — Wizard-Zwischenstaende inkl. Eingaben
  - telegram_termine_seen       — welcher Chat welche Termine gesehen hat
  - belege.chat_id              — Herkunfts-Chat eines Belegs
  - visualisierungen.chat_id / rechnungen.chat_id — dito
  - tool_configs telegram_bot / telegram_notify — enthalten den
    verschluesselten Bot-Token

Reste ohne Personenbezug:
  - tenants.onboarding_step / onboarding_completed_at (Bot-Tutorial)
  - health_check_results.telegram_ok (geprueft wurde der Bot)
  - belege.source: server_default 'telegram' -> 'api'

Downgrade stellt die Struktur wieder her, aber NICHT die Daten — die
sind bewusst weg.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5w2y8k4n6q1"
down_revision: Union[str, None] = "c3t9r5p2m8q4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    # --- Bot-Token + Feature-Flags aus der Config raeumen -------------
    op.execute(
        "DELETE FROM tool_configs "
        "WHERE tool_name IN ('telegram_bot', 'telegram_notify')"
    )

    # --- Wizard-/Merk-Tabellen ---------------------------------------
    op.execute("DROP TABLE IF EXISTS telegram_termine_seen")
    op.execute("DROP TABLE IF EXISTS telegram_state")

    # --- Chat-IDs (Personenbezug) ------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_emp_chat")
    op.execute("ALTER TABLE employees DROP COLUMN IF EXISTS telegram_chat_id")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS telegram_chat_id")
    op.execute("ALTER TABLE belege DROP COLUMN IF EXISTS chat_id")
    op.execute("ALTER TABLE visualisierungen DROP COLUMN IF EXISTS chat_id")
    op.execute("ALTER TABLE rechnungen DROP COLUMN IF EXISTS chat_id")

    # --- Bot-Tutorial + Bot-Healthcheck ------------------------------
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS onboarding_step")
    op.execute(
        "ALTER TABLE tenants DROP COLUMN IF EXISTS onboarding_completed_at"
    )
    op.execute(
        "ALTER TABLE health_check_results DROP COLUMN IF EXISTS telegram_ok"
    )

    # --- Beleg-Quelle: neue Belege kommen aus App/Mail ----------------
    op.execute("ALTER TABLE belege ALTER COLUMN source SET DEFAULT 'api'")


def downgrade() -> None:
    """Stellt die Struktur wieder her — die Daten sind bewusst weg.

    Bewusst mit IF NOT EXISTS: der Upgrade-Pfad wurde auf Prod in zwei
    Etappen gefahren, ein starres ADD COLUMN wuerde dort auf bereits
    vorhandene Spalten laufen.
    """
    op.execute("ALTER TABLE belege ALTER COLUMN source SET DEFAULT 'telegram'")
    op.execute(
        "ALTER TABLE health_check_results ADD COLUMN IF NOT EXISTS "
        "telegram_ok BOOLEAN NOT NULL DEFAULT TRUE"
    )
    op.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
        "onboarding_completed_at TIMESTAMPTZ NULL"
    )
    op.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
        "onboarding_step INTEGER NOT NULL DEFAULT 0"
    )
    for tabelle in ("belege", "visualisierungen", "rechnungen"):
        op.execute(
            f"ALTER TABLE {tabelle} ADD COLUMN IF NOT EXISTS chat_id BIGINT NULL"
        )
    op.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT NULL"
    )
    op.execute(
        "ALTER TABLE employees ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_emp_chat ON employees (telegram_chat_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenants_telegram_chat_id "
        "ON tenants (telegram_chat_id)"
    )
