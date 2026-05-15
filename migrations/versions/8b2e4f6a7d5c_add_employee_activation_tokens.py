"""employee_activation_tokens-Tabelle fuer Mitarbeiter-Onboarding-Links.

Revision ID: 8b2e4f6a7d5c
Revises: z5ac6od8p1q3
Create Date: 2026-05-15 19:00:00.000000

Phase-2-Schritt: ein neu angelegter Mitarbeiter bekommt einen
One-Time-Use-Token, der per Telegram-Deep-Link
(`https://t.me/{bot_username}?start=activate_{token}`) den
Onboarding-Flow startet. Token-Laenge 64 Zeichen (secrets.token_urlsafe
mit 48 Bytes), Gueltigkeit 7 Tage, used_at NULL bis Mitarbeiter den
Link einloest.

Additiv. Keine Backfill noetig (Tabelle startet leer).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8b2e4f6a7d5c"
down_revision: Union[str, None] = "z5ac6od8p1q3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_activation_tokens",
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
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token", sa.String(length=64), nullable=False, unique=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "used_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.create_index(
        "ix_activation_employee", "employee_activation_tokens",
        ["employee_id"],
    )
    op.create_index(
        "ix_activation_tenant", "employee_activation_tokens",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_activation_tenant", table_name="employee_activation_tokens")
    op.drop_index("ix_activation_employee", table_name="employee_activation_tokens")
    op.drop_table("employee_activation_tokens")
