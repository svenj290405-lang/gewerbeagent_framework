"""Add voice_calls + telegram_anrufe_seen (und Merge der 3 offenen Heads)

Revision ID: c5a9f1d7b3e2
Revises: e636dec91e7a, q3l7h2j5g9k4, u8j2m5q9r3t6
Create Date: 2026-05-30 12:00:00.000000

Zwei Zwecke in einer Migration:

1. MERGE: develop hatte drei unverbundene Alembic-Heads
   (tenant_anfrage_schemas, oauth_per_employee, health_check_timestamps).
   `alembic upgrade head` waere daran gescheitert. down_revision listet
   alle drei -> diese Migration ist der neue gemeinsame Head.

2. SCHEMA: Tabellen fuer das neue /anrufe (eingehende ElevenLabs-Telefonate):
   - voice_calls: ein Eintrag pro Anruf (geschrieben vom /call_ended-Webhook)
   - telegram_anrufe_seen: Wasserstand pro Chat fuer "seit letztem Aufruf"
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c5a9f1d7b3e2'
down_revision: Union[str, Sequence[str], None] = (
    'e636dec91e7a',
    'q3l7h2j5g9k4',
    'u8j2m5q9r3t6',
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'voice_calls',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),

        # Telefonie-Metadaten (alle optional)
        sa.Column('caller_number', sa.String(50), nullable=True,
                  comment='Anrufer-Nummer (caller_id) fuer Rueckruf'),
        sa.Column('called_number', sa.String(50), nullable=True,
                  comment='Angerufene Tenant-Nummer'),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('outcome', sa.String(30), nullable=True,
                  comment='completed, incomplete, no_audio'),
        sa.Column('conversation_id', sa.String(200), nullable=True,
                  comment='ElevenLabs-Conversation-ID'),

        # Optional vom Agent erfasst
        sa.Column('kunde_name', sa.String(300), nullable=True),
        sa.Column('anliegen', sa.String(500), nullable=True),
        sa.Column('zusammenfassung', sa.Text(), nullable=True),

        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),

        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    )
    # /anrufe-Liste: Tenant-Anrufe neueste zuerst, gefiltert "seit letztem Mal"
    op.create_index(
        'ix_voice_calls_tenant_created',
        'voice_calls',
        ['tenant_id', 'created_at'],
    )

    op.create_table(
        'telegram_anrufe_seen',
        sa.Column('chat_id', sa.BigInteger(), primary_key=True),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('telegram_anrufe_seen')
    op.drop_index('ix_voice_calls_tenant_created', table_name='voice_calls')
    op.drop_table('voice_calls')
