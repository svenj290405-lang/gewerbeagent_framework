"""anfrage_tokens/anfrage_responses: fehlende created_at/updated_at ergaenzen

Schema-Drift-Fix: Base (core/database/base.py) fuegt allen Tabellen
created_at UND updated_at hinzu. Die urspruengliche Migration fuer die
Anfrage-Formulare (2decee0aa2ec) legte aber nur:
  - anfrage_tokens.created_at an (updated_at fehlt — wird von Base geerbt)
  - anfrage_responses.submitted_at an (created_at UND updated_at fehlen)

Dadurch bricht ein aus den Migrationen frisch aufgebautes Schema beim
INSERT/RETURNING (ORM erwartet die Base-Spalten). Die laufende Prod-DB
hat die Spalten bereits out-of-band -> diese Migration ist dort no-op.

Idempotent via ``ADD COLUMN IF NOT EXISTS`` (Postgres), damit sie sowohl
auf der bereits gepatchten Prod-DB als auch auf frischen DBs sauber
durchlaeuft. server_default now() fuellt eventuelle Bestands-Zeilen;
onupdate ist ORM-seitig (Base), kein DDL noetig.

Revision ID: c7e9a1b3d5f2
Revises: u8j2m5q9r3t6
Create Date: 2026-05-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c7e9a1b3d5f2"
down_revision: Union[str, Sequence[str], None] = "u8j2m5q9r3t6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE anfrage_tokens "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    )
    op.execute(
        "ALTER TABLE anfrage_responses "
        "ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    )
    op.execute(
        "ALTER TABLE anfrage_responses "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE anfrage_responses DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE anfrage_responses DROP COLUMN IF EXISTS created_at")
    op.execute("ALTER TABLE anfrage_tokens DROP COLUMN IF EXISTS updated_at")
