"""rechnung bezahl tracking

Revision ID: j2c8e5f1a4d6
Revises: i7a3b2d8c1e9
Create Date: 2026-05-10 09:00:00.000000

Erweitert die rechnungen-Tabelle um Bezahl-Tracking-Felder, damit der
Lexware-Voucher-Status periodisch gepollt werden kann und einmal pro Tag
um 18:00 eine Telegram-Tages-Zusammenfassung der bezahlten Rechnungen
herausgehen kann.

Additive Migration: nur neue, nullable Spalten + ein Composite-Index.
Keine bestehenden Werte werden veraendert.

Felder:
- bezahlt_am: wann Lexware "paid" gemeldet hat (UTC, nullable)
- lexware_voucher_status: zuletzt von Lexware gemeldeter voucherStatus
  ("draft", "open", "paid", "voided", "unknown"). Cache, damit man sieht
  "Lexware sagt aktuell open" ohne erneuten API-Call.
- last_paid_check_at: wann zuletzt gegen Lexware gepollt wurde. Erlaubt
  Diagnose wenn ein Tenant-API-Key tot ist (last_check ist alt).
- paid_notification_sent: ob die 18:00-Tages-Zusammenfassung bereits
  diesen Bezahl-Eintrag enthalten hat. Verhindert Doppel-Notify wenn
  der Cron-Lauf an einem Folgetag nochmal die selbe Rechnung sieht.

Index (status, bezahlt_am): das Polling-SELECT laeuft mit
WHERE status = 'mail_sent' AND bezahlt_am IS NULL und scaled mit der
Anzahl offener Rechnungen, nicht mit der Gesamtmenge.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "j2c8e5f1a4d6"
down_revision = "i7a3b2d8c1e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rechnungen",
        sa.Column(
            "bezahlt_am",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "rechnungen",
        sa.Column(
            "lexware_voucher_status",
            sa.String(length=30),
            nullable=True,
        ),
    )
    op.add_column(
        "rechnungen",
        sa.Column(
            "last_paid_check_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "rechnungen",
        sa.Column(
            "paid_notification_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Index fuer das Polling-SELECT: alle "mail_sent"-Rechnungen die noch
    # nicht als bezahlt markiert sind. Bei wachsender DB sehr wichtig,
    # weil das alle 30 Min ueber alle Tenants laeuft.
    op.create_index(
        "ix_rechnungen_status_bezahlt_am",
        "rechnungen",
        ["status", "bezahlt_am"],
    )

    # Index fuer die Tages-Zusammenfassung: pro Tenant alle Rechnungen,
    # die heute bezahlt wurden und noch nicht gepusht waren.
    op.create_index(
        "ix_rechnungen_paid_notify",
        "rechnungen",
        ["tenant_id", "bezahlt_am", "paid_notification_sent"],
    )


def downgrade() -> None:
    op.drop_index("ix_rechnungen_paid_notify", table_name="rechnungen")
    op.drop_index("ix_rechnungen_status_bezahlt_am", table_name="rechnungen")
    op.drop_column("rechnungen", "paid_notification_sent")
    op.drop_column("rechnungen", "last_paid_check_at")
    op.drop_column("rechnungen", "lexware_voucher_status")
    op.drop_column("rechnungen", "bezahlt_am")
