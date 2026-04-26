"""add email_conversations

Revision ID: fe063beab4c7
Revises: 566993123044
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "fe063beab4c7"
down_revision: Union[str, None] = "566993123044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_conversations",
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
        sa.Column("kunde_email", sa.String(255), nullable=False),
        sa.Column("kunde_name", sa.String(255), nullable=True),
        sa.Column("gcal_event_id", sa.String(255), nullable=True),
        sa.Column("termin_datum", sa.Date(), nullable=True),
        sa.Column("last_message_id", sa.String(500), nullable=True),
        sa.Column(
            "state",
            sa.String(50),
            nullable=False,
            server_default="awaiting_confirmation",
        ),
        sa.Column("proposed_slots", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_subject", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_email_conv_tenant_kunde",
        "email_conversations",
        ["tenant_id", "kunde_email"],
    )
    op.create_index(
        "ix_email_conv_message_id",
        "email_conversations",
        ["last_message_id"],
    )
    op.create_index(
        "ix_email_conv_termin_datum",
        "email_conversations",
        ["termin_datum"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_conv_termin_datum", table_name="email_conversations")
    op.drop_index("ix_email_conv_message_id", table_name="email_conversations")
    op.drop_index("ix_email_conv_tenant_kunde", table_name="email_conversations")
    op.drop_table("email_conversations")
