"""rueckrufe: strukturierte Rueckrufbitten aus dem Voice-Agent

Legt die Tabelle ``rueckrufe`` an (siehe core/models/rueckruf.py).

Diese Migration verschmilzt zugleich die zwei offenen Alembic-Heads
``c7e9a1b3d5f2`` (anfrage_updated_at) und ``d2n6q8s4t1v6``
(tenant_drive_root_folder_id) — der Graph war gebrancht ohne
Merge-Knoten, ``alembic upgrade head`` haette sonst "multiple heads"
geworfen. down_revision ist daher ein Tupel beider Heads.

Revision ID: b3k7n2p9r4t1
Revises: c7e9a1b3d5f2, d2n6q8s4t1v6
Create Date: 2026-06-02 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "b3k7n2p9r4t1"
down_revision: Union[str, Sequence[str], None] = (
    "c7e9a1b3d5f2",
    "d2n6q8s4t1v6",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rueckrufe",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, nullable=False,
        ),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("kunde_name", sa.String(length=300), nullable=False),
        sa.Column("kunde_telefon", sa.String(length=50), nullable=False),
        sa.Column("anliegen", sa.Text(), nullable=False),
        sa.Column("kunde_email", sa.String(length=255), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False,
            server_default="offen",
        ),
        sa.Column(
            "assigned_employee_id", UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("erledigt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "erledigt_by_employee_id", UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_rueckrufe_tenant_id", "rueckrufe", ["tenant_id"],
    )
    op.create_index(
        "ix_rueckrufe_status", "rueckrufe", ["status"],
    )
    op.create_index(
        "ix_rueckrufe_assigned_employee_id", "rueckrufe",
        ["assigned_employee_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_rueckrufe_assigned_employee_id", table_name="rueckrufe")
    op.drop_index("ix_rueckrufe_status", table_name="rueckrufe")
    op.drop_index("ix_rueckrufe_tenant_id", table_name="rueckrufe")
    op.drop_table("rueckrufe")
