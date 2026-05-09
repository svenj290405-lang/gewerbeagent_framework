"""Add admin auth + api usage/pricing tables

Revision ID: i7a3b2d8c1e9
Revises: 0dab69a22af1
Create Date: 2026-05-10 02:00:00.000000

Erstellt:
- admin_users, admin_sessions, admin_audit_log, admin_login_attempts
- api_pricing_config, api_usage_log
- Seed-Daten fuer Pricing-Config (Stand 09.05.2026)

Alle Tabellen sind additiv. Kein DROP.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "i7a3b2d8c1e9"
down_revision: Union[str, Sequence[str], None] = "0dab69a22af1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------
# Seed-Preise (Stand 09.05.2026)
# Werte stammen aus den oeffentlichen Preislisten der Anbieter,
# umgerechnet zum aktuellen EUR-Kurs. Sven kann sie spaeter ueber
# /admin/pricing editieren.
#
# Format: (provider, operation, unit, price_per_unit_eur, notes)
# ---------------------------------------------------------------
SEED_PRICING = [
    # Gemini 2.5 Flash (Vertex AI europe-west3)
    # ~$0.30 / 1M input tokens, ~$2.50 / 1M output tokens
    ("gemini", "gemini-2.5-flash", "input_token",
     "0.00000028", "0.30 USD / 1M tokens, EUR Kurs ~0.93"),
    ("gemini", "gemini-2.5-flash", "output_token",
     "0.00000232", "2.50 USD / 1M tokens"),
    ("gemini", "gemini-2.5-flash", "cached_input_token",
     "0.00000007", "0.075 USD / 1M tokens"),
    ("gemini", "gemini-2.5-flash-image", "request",
     "0.030", "geschaetzt 0.03 EUR pro Bildgenerierung"),

    # ElevenLabs - Standard TTS, ca. 0.18 USD pro 1k Zeichen
    ("elevenlabs", "tts-default", "character",
     "0.000167", "0.18 USD / 1k chars (Pro Plan)"),

    # Deepgram - Nova-3 Streaming, ca. 0.0058 USD pro Sekunde
    ("deepgram", "nova-3-streaming", "second",
     "0.005370", "0.0058 USD / sec, EUR Kurs ~0.93"),

    # Sipgate - Inbound Festnetz Deutschland: ~0 ct/min, Mobile gemittelt
    ("sipgate", "inbound-de", "second",
     "0.000000", "Inbound deutsche Festnetznummer kostenfrei"),
    ("sipgate", "outbound-de", "second",
     "0.000150", "ca. 0.9 ct/min Festnetz DE"),

    # Microsoft Graph / Outlook 365 Business
    ("microsoft", "mail-send", "mail_send",
     "0.000000", "Outlook 365 Lizenz separat, kein per-Mail Preis"),
    ("microsoft", "graph-api", "request",
     "0.000000", "Graph-API innerhalb Lizenz inkludiert"),

    # Brevo Transaktional - 5000 Mails inkludiert, danach 0.001 EUR/Mail
    ("brevo", "transactional-mail", "mail_send",
     "0.001000", "Pay-as-you-go nach Inkludiert"),

    # Lexware Office API - kostenfrei in Office Plus
    ("lexware", "api-call", "request",
     "0.000000", "Office Plus inkludiert"),

    # Telegram Bot API - kostenlos
    ("telegram", "bot-api", "request",
     "0.000000", "Telegram Bot API ist kostenfrei"),
]


def upgrade() -> None:
    # ---------- admin_users ----------
    op.create_table(
        "admin_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_ip", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_admin_users_email", "admin_users", ["email"])

    # ---------- admin_sessions ----------
    op.create_table(
        "admin_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_admin_sessions_token", "admin_sessions", ["token"])
    op.create_index("ix_admin_sessions_user_id", "admin_sessions", ["user_id"])

    # ---------- admin_audit_log ----------
    op.create_table(
        "admin_audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target", sa.String(255), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("details", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_admin_audit_user_id", "admin_audit_log", ["user_id"])
    op.create_index("ix_admin_audit_action", "admin_audit_log", ["action"])

    # ---------- admin_login_attempts ----------
    op.create_table(
        "admin_login_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("ip_address", sa.String(64), nullable=False),
        sa.Column("email_tried", sa.String(255), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("attempted_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_login_attempts_ip", "admin_login_attempts", ["ip_address"])
    op.create_index("ix_login_attempts_attempted", "admin_login_attempts", ["attempted_at"])
    op.create_index("ix_login_attempts_ip_time", "admin_login_attempts",
                    ["ip_address", "attempted_at"])

    # ---------- api_pricing_config ----------
    op.create_table(
        "api_pricing_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("operation", sa.String(80), nullable=True),
        sa.Column("unit", sa.String(50), nullable=False),
        sa.Column("price_per_unit_eur", sa.Numeric(18, 10), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pricing_provider", "api_pricing_config", ["provider"])
    op.create_index("ix_pricing_lookup", "api_pricing_config",
                    ["provider", "operation", "unit", "valid_from"])

    # ---------- api_usage_log ----------
    op.create_table(
        "api_usage_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("operation", sa.String(80), nullable=True),
        sa.Column("unit", sa.String(50), nullable=False),
        sa.Column("units_consumed", sa.Numeric(18, 6), nullable=False),
        sa.Column("price_per_unit_eur", sa.Numeric(18, 10), nullable=True),
        sa.Column("cost_eur", sa.Numeric(18, 8), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("pricing_config_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("api_pricing_config.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("request_id", sa.String(120), nullable=True),
        sa.Column("metadata", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_usage_tenant_id", "api_usage_log", ["tenant_id"])
    op.create_index("ix_usage_provider", "api_usage_log", ["provider"])
    op.create_index("ix_usage_tenant_time", "api_usage_log",
                    ["tenant_id", "created_at"])
    op.create_index("ix_usage_provider_time", "api_usage_log",
                    ["provider", "created_at"])

    # ---------- Seed Pricing ----------
    pricing_table = sa.table(
        "api_pricing_config",
        sa.column("provider", sa.String),
        sa.column("operation", sa.String),
        sa.column("unit", sa.String),
        sa.column("price_per_unit_eur", sa.Numeric),
        sa.column("notes", sa.Text),
    )
    op.bulk_insert(
        pricing_table,
        [
            {
                "provider": p,
                "operation": o,
                "unit": u,
                "price_per_unit_eur": Decimal(price),
                "notes": notes,
            }
            for (p, o, u, price, notes) in SEED_PRICING
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_provider_time", table_name="api_usage_log")
    op.drop_index("ix_usage_tenant_time", table_name="api_usage_log")
    op.drop_index("ix_usage_provider", table_name="api_usage_log")
    op.drop_index("ix_usage_tenant_id", table_name="api_usage_log")
    op.drop_table("api_usage_log")

    op.drop_index("ix_pricing_lookup", table_name="api_pricing_config")
    op.drop_index("ix_pricing_provider", table_name="api_pricing_config")
    op.drop_table("api_pricing_config")

    op.drop_index("ix_login_attempts_ip_time", table_name="admin_login_attempts")
    op.drop_index("ix_login_attempts_attempted", table_name="admin_login_attempts")
    op.drop_index("ix_login_attempts_ip", table_name="admin_login_attempts")
    op.drop_table("admin_login_attempts")

    op.drop_index("ix_admin_audit_action", table_name="admin_audit_log")
    op.drop_index("ix_admin_audit_user_id", table_name="admin_audit_log")
    op.drop_table("admin_audit_log")

    op.drop_index("ix_admin_sessions_user_id", table_name="admin_sessions")
    op.drop_index("ix_admin_sessions_token", table_name="admin_sessions")
    op.drop_table("admin_sessions")

    op.drop_index("ix_admin_users_email", table_name="admin_users")
    op.drop_table("admin_users")
