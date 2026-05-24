"""employee_activation_tokens: fehlende updated_at-Spalte ergaenzen

Schema-Drift-Fix: Base (core/database/base.py) fuegt allen Tabellen
created_at UND updated_at hinzu, aber die urspruengliche Migration fuer
employee_activation_tokens (8b2e4f6a7d5c) hat updated_at vergessen. Dadurch
schlug `create_activation_token` beim INSERT ... RETURNING updated_at mit
"column ... updated_at does not exist" fehl — das token-basierte
Mitarbeiter-Onboarding fiel still auf den Legacy-Slug-Link zurueck. Mit
S13 (Slug-Bindung abgeschaltet) gibt es diesen Fallback nicht mehr, daher
muss die Spalte jetzt existieren.

Additiv: ADD COLUMN mit server_default now() — Bestands-Zeilen (falls
vorhanden) bekommen now(). onupdate ist ORM-seitig (Base), kein DDL noetig.

Revision ID: b8d2f4a6c1e3
Revises: f3a7c1e9b2d4
Create Date: 2026-05-24 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8d2f4a6c1e3"
down_revision: Union[str, Sequence[str], None] = "f3a7c1e9b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "employee_activation_tokens",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("employee_activation_tokens", "updated_at")
