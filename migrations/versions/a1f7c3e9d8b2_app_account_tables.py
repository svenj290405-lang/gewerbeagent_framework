"""app-account: PWA-Auth + Web-Push-Tabellen

Legt die drei Tabellen fuer die Inhaber-/Mitarbeiter-PWA an
(siehe core/models/app_account.py):
- app_sessions        Server-side Web-Sessions (an employees gebunden)
- app_login_tokens    Magic-Link-Token (passwortloser Login)
- push_subscriptions  Web-Push-Abos (VAPID)

Alle drei erben created_at/updated_at von Base (hier explizit aufgefuehrt,
Base-Falle).

Revision ID: a1f7c3e9d8b2
Revises: b3k7n2p9r4t1
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "a1f7c3e9d8b2"
down_revision: Union[str, Sequence[str], None] = "b3k7n2p9r4t1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    ]


def upgrade() -> None:
    # --- app_sessions ---
    op.create_table(
        "app_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "employee_id", UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column(
            "last_activity_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "revoked", sa.Boolean(), server_default=sa.false(), nullable=False,
        ),
        *_timestamps(),
    )
    op.create_index("ix_app_sessions_employee_id", "app_sessions", ["employee_id"])
    op.create_index("ix_app_sessions_tenant_id", "app_sessions", ["tenant_id"])
    op.create_index(
        "ix_app_sessions_token", "app_sessions", ["token"], unique=True,
    )

    # --- app_login_tokens ---
    op.create_table(
        "app_login_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "employee_id", UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "ix_app_login_tokens_employee_id", "app_login_tokens", ["employee_id"],
    )
    op.create_index(
        "ix_app_login_tokens_tenant_id", "app_login_tokens", ["tenant_id"],
    )
    op.create_index(
        "ix_app_login_tokens_token", "app_login_tokens", ["token"], unique=True,
    )

    # --- push_subscriptions ---
    op.create_table(
        "push_subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "employee_id", UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("endpoint", sa.String(length=2048), nullable=False),
        sa.Column("p256dh", sa.String(length=255), nullable=False),
        sa.Column("auth", sa.String(length=255), nullable=False),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        *_timestamps(),
    )
    op.create_unique_constraint(
        "uq_push_sub_endpoint", "push_subscriptions", ["endpoint"],
    )
    op.create_index("ix_push_sub_employee", "push_subscriptions", ["employee_id"])
    op.create_index("ix_push_sub_tenant", "push_subscriptions", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_push_sub_tenant", table_name="push_subscriptions")
    op.drop_index("ix_push_sub_employee", table_name="push_subscriptions")
    op.drop_constraint(
        "uq_push_sub_endpoint", "push_subscriptions", type_="unique",
    )
    op.drop_table("push_subscriptions")

    op.drop_index("ix_app_login_tokens_token", table_name="app_login_tokens")
    op.drop_index("ix_app_login_tokens_tenant_id", table_name="app_login_tokens")
    op.drop_index("ix_app_login_tokens_employee_id", table_name="app_login_tokens")
    op.drop_table("app_login_tokens")

    op.drop_index("ix_app_sessions_token", table_name="app_sessions")
    op.drop_index("ix_app_sessions_tenant_id", table_name="app_sessions")
    op.drop_index("ix_app_sessions_employee_id", table_name="app_sessions")
    op.drop_table("app_sessions")
