"""Add tenant_leistungen + angebote + angebot_positionen for offer-pipeline

Revision ID: 0131127e9dc8
Revises: h2c5e8a1f7d3
Create Date: 2026-05-04 13:30:00.000000

Drei neue Tabellen fuer die Angebots-Pipeline:

1. tenant_leistungen - Wissensbasis: Was bietet der Handwerker an? Mit Preisen,
   Einheiten, Standard-Beschreibungen. Gemini nutzt das beim Angebots-Erstellen
   um Preise zu matchen + Texte zu polieren.

2. angebote - Header der Angebote (Kunde, Gesamtbetrag, Lexware-IDs, AI-Texte)

3. angebot_positionen - Einzelne Positionen pro Angebot, optional verknuepft
   zur tenant_leistungen-Vorlage.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0131127e9dc8'
down_revision: Union[str, Sequence[str], None] = 'h2c5e8a1f7d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ============================================================
    # tenant_leistungen - Wissensbasis: Leistungen + Preise
    # ============================================================
    op.create_table(
        'tenant_leistungen',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),

        sa.Column('name', sa.String(200), nullable=False,
                  comment='Hauptbezeichnung der Leistung, z.B. "Moebelmontage"'),
        sa.Column('aliase', postgresql.ARRAY(sa.Text()), nullable=True,
                  comment='Synonyme fuer Voice-Matching, z.B. ["Moebel aufbauen", "Schrank montieren"]'),

        sa.Column('preis_eur', sa.Numeric(10, 2), nullable=False,
                  comment='Preis pro Einheit in EUR (brutto)'),
        sa.Column('einheit', sa.String(50), nullable=False,
                  comment='Stunde, Stueck, lfm, m2, pauschal, etc.'),
        sa.Column('mwst_prozent', sa.Integer(), nullable=False, server_default='19',
                  comment='Standard MwSt 19, ermaessigt 7, Kleinunternehmer 0'),

        sa.Column('standard_beschreibung', sa.Text(), nullable=True,
                  comment='Vorlagentext fuer line-description bei Angeboten/Rechnungen'),

        sa.Column('aktiv', sa.Boolean(), nullable=False, server_default=sa.text('TRUE'),
                  comment='Soft-delete: aktiv=FALSE laesst alte Verknuepfungen unberuehrt'),
        sa.Column('sortierung', sa.Integer(), nullable=False, server_default='0'),

        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),

        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    )
    op.create_index(
        'ix_tenant_leistungen_aktiv',
        'tenant_leistungen',
        ['tenant_id'],
        postgresql_where=sa.text('aktiv = TRUE'),
    )
    op.create_index(
        'ix_tenant_leistungen_name_lower',
        'tenant_leistungen',
        ['tenant_id', sa.text('LOWER(name)')],
    )

    # ============================================================
    # angebote - Header der Angebote
    # ============================================================
    op.create_table(
        'angebote',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),

        # Quelle
        sa.Column('quelle', sa.String(20), nullable=False,
                  comment='telegram_voice, telegram_text, voice_call (zukuenftig)'),
        sa.Column('raw_input', sa.Text(), nullable=True,
                  comment='Voice-Transkript oder Text-Input vom Tenant'),

        # Kundendaten
        sa.Column('kunde_name', sa.String(300), nullable=False),
        sa.Column('kunde_strasse', sa.String(300), nullable=True),
        sa.Column('kunde_plz', sa.String(20), nullable=True),
        sa.Column('kunde_ort', sa.String(200), nullable=True),

        # Gesamtbetrag (errechnet aus Positionen, gespeichert fuer Stats)
        sa.Column('gesamtbetrag_brutto_eur', sa.Numeric(10, 2), nullable=True),

        # Lexware-Anbindung
        sa.Column('lexware_quotation_id', postgresql.UUID(as_uuid=True), nullable=True,
                  comment='UUID des Lexware-Angebots nach Anlage'),
        sa.Column('lexware_voucher_number', sa.String(50), nullable=True,
                  comment='Lexware-Belegnummer wie AN-00042'),
        sa.Column('lexware_status', sa.String(50), nullable=True,
                  comment='draft, open, accepted, rejected, etc. (von Lexware)'),

        # AI-generierte Texte
        sa.Column('introduction_text', sa.Text(), nullable=True,
                  comment='Anschreiben/Einleitung von Gemini'),
        sa.Column('remark_text', sa.Text(), nullable=True,
                  comment='Schlussbemerkung/Footer von Gemini'),

        # Workflow
        sa.Column('status', sa.String(50), nullable=False, server_default="'erstellt'",
                  comment='erstellt, in_lexware, finalisiert, storniert'),

        sa.Column('confidence', sa.String(20), nullable=True,
                  comment='Gemini-Confidence: high, medium, low'),

        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),

        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    )
    op.create_index(
        'ix_angebote_tenant_created',
        'angebote',
        ['tenant_id', sa.text('created_at DESC')],
    )
    op.create_index(
        'ix_angebote_lexware_quotation',
        'angebote',
        ['lexware_quotation_id'],
        postgresql_where=sa.text('lexware_quotation_id IS NOT NULL'),
    )

    # ============================================================
    # angebot_positionen - Einzelne Positionen pro Angebot
    # ============================================================
    op.create_table(
        'angebot_positionen',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('angebot_id', postgresql.UUID(as_uuid=True), nullable=False),

        sa.Column('position_nr', sa.Integer(), nullable=False,
                  comment='Reihenfolge im Angebot, 1-basiert'),

        sa.Column('name', sa.String(500), nullable=False,
                  comment='Kurzname der Position'),
        sa.Column('beschreibung', sa.Text(), nullable=True,
                  comment='Lange Beschreibung fuer Lexware lineDescription, von Gemini oder Wissensbasis'),

        sa.Column('menge', sa.Numeric(12, 3), nullable=False, server_default='1'),
        sa.Column('einheit', sa.String(50), nullable=False, server_default="'Stueck'"),
        sa.Column('preis_brutto_eur', sa.Numeric(10, 2), nullable=False),
        sa.Column('mwst_prozent', sa.Integer(), nullable=False, server_default='19'),

        # Verknuepfung zur Wissensbasis (fuer Stats: welche Leistungen oft verkauft?)
        sa.Column('leistung_id', postgresql.UUID(as_uuid=True), nullable=True,
                  comment='Optional: aus welcher Wissensbasis-Vorlage entstanden'),

        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),

        sa.ForeignKeyConstraint(['angebot_id'], ['angebote.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['leistung_id'], ['tenant_leistungen.id'], ondelete='SET NULL'),
    )
    op.create_index(
        'ix_angebot_positionen_unique',
        'angebot_positionen',
        ['angebot_id', 'position_nr'],
        unique=True,
    )
    op.create_index(
        'ix_angebot_positionen_leistung',
        'angebot_positionen',
        ['leistung_id'],
        postgresql_where=sa.text('leistung_id IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_angebot_positionen_leistung', table_name='angebot_positionen')
    op.drop_index('ix_angebot_positionen_unique', table_name='angebot_positionen')
    op.drop_table('angebot_positionen')

    op.drop_index('ix_angebote_lexware_quotation', table_name='angebote')
    op.drop_index('ix_angebote_tenant_created', table_name='angebote')
    op.drop_table('angebote')

    op.drop_index('ix_tenant_leistungen_name_lower', table_name='tenant_leistungen')
    op.drop_index('ix_tenant_leistungen_aktiv', table_name='tenant_leistungen')
    op.drop_table('tenant_leistungen')
