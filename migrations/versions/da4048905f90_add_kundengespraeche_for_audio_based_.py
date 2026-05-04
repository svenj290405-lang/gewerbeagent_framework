"""Add kundengespraeche for audio-based briefings

Revision ID: da4048905f90
Revises: 0131127e9dc8
Create Date: 2026-05-04 14:30:00.000000

Tabelle fuer aufgezeichnete Kundengespraeche von Dietz beim Kunden.
Workflow:
1. Dietz nimmt Gespraech auf (Telegram /aufnahme)
2. Gemini analysiert: Transkript + Briefing + Positionen + Termin
3. Speicherung hier
4. Optional: Lexware-Angebot draus erstellen (verknuepft via angebot_id)
5. Briefing-Befehle (/briefing, /kunde X) lesen aus dieser Tabelle
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'da4048905f90'
down_revision: Union[str, Sequence[str], None] = '0131127e9dc8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'kundengespraeche',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),

        # Kundendaten (aus Audio extrahiert)
        sa.Column('kunde_name', sa.String(300), nullable=False,
                  comment='Aus Audio extrahiert, "Frau Mueller" o.ae.'),
        sa.Column('kunde_kontakt_id', postgresql.UUID(as_uuid=True), nullable=True,
                  comment='Lexware-Kontakt-UUID falls upsert geklappt hat'),

        # Audio-Metadaten
        sa.Column('gespraech_datum', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(),
                  comment='Wann fand das Gespraech statt (default: Aufnahme-Zeit)'),
        sa.Column('audio_dauer_sekunden', sa.Integer(), nullable=True,
                  comment='Laenge der Aufnahme - fuer Stats und Token-Kalkulation'),
        sa.Column('raw_transcript', sa.Text(), nullable=True,
                  comment='Vollstaendiges Gemini-Transkript des Gespraechs'),

        # Strukturierte Daten fuer Briefing
        sa.Column('briefing_kurz', sa.Text(), nullable=True,
                  comment='3-5 Saetze fuer Pre-Termin-Briefing (was Dietz wissen muss)'),
        sa.Column('notizen_lang', sa.Text(), nullable=True,
                  comment='Vollstaendige Notizen, alles Wichtige aus dem Gespraech'),
        sa.Column('todos', postgresql.ARRAY(sa.Text()), nullable=True,
                  comment='Was Dietz erledigen muss: Material, Kollege mitbringen, etc.'),

        # Termin (falls im Gespraech vereinbart)
        sa.Column('termin_datum', sa.DateTime(timezone=True), nullable=True),
        sa.Column('termin_ort', sa.String(300), nullable=True,
                  comment='Falls Termin-Adresse abweicht von Kunden-Adresse'),

        # Verknuepfung zu Angebot
        sa.Column('angebot_id', postgresql.UUID(as_uuid=True), nullable=True),

        # Workflow + Qualitaet
        sa.Column('confidence', sa.String(20), nullable=True,
                  comment='Gemini-Confidence: high, medium, low'),
        sa.Column('status', sa.String(50), nullable=False,
                  server_default='erfasst',
                  comment='erfasst, mit_angebot, archiviert'),

        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),

        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['angebot_id'], ['angebote.id'], ondelete='SET NULL'),
    )

    # Index fuer /anrufe-Liste (neueste zuerst)
    op.create_index(
        'ix_kundengespraeche_tenant_datum',
        'kundengespraeche',
        ['tenant_id', sa.text('gespraech_datum DESC')],
    )

    # Index fuer Termin-Briefing-Lookup (anstehende Termine)
    op.create_index(
        'ix_kundengespraeche_termin',
        'kundengespraeche',
        ['tenant_id', 'termin_datum'],
        postgresql_where=sa.text('termin_datum IS NOT NULL'),
    )

    # Index fuer Kunden-Lookup ("/kunde Mueller")
    op.create_index(
        'ix_kundengespraeche_kunde_name',
        'kundengespraeche',
        ['tenant_id', sa.text('LOWER(kunde_name)')],
    )


def downgrade() -> None:
    op.drop_index('ix_kundengespraeche_kunde_name', table_name='kundengespraeche')
    op.drop_index('ix_kundengespraeche_termin', table_name='kundengespraeche')
    op.drop_index('ix_kundengespraeche_tenant_datum', table_name='kundengespraeche')
    op.drop_table('kundengespraeche')
