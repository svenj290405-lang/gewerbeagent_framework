"""employees: app_password_hash fuer klassisches PWA-Passwort-Login

Fuegt eine nullable Spalte employees.app_password_hash hinzu (bcrypt-Hash).
NULL = kein Passwort gesetzt (nur Magic-Link/Login-Link).

Revision ID: c4e8a2b6f1d9
Revises: a1f7c3e9d8b2
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c4e8a2b6f1d9"
down_revision: Union[str, Sequence[str], None] = "a1f7c3e9d8b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("app_password_hash", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("employees", "app_password_hash")
