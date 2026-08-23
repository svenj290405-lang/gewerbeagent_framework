"""Besucherzaehlung der Website: Rohereignisse, Tagessummen, Tages-Salt

Revision ID: k3m6p9r2t5w8
Revises: j2k5n8q1r4t7
Create Date: 2026-08-23 23:20:00.000000

Eigene, cookielose Reichweitenmessung — siehe core/models/website_visit.py.
Rohereignisse leben 14 Tage (wie die Server-Logs laut Datenschutz-
erklaerung), die Tagessummen bleiben dauerhaft und enthalten keinen
Personenbezug mehr, der Tages-Salt wird nach zwei Tagen geloescht.

Alle drei Tabellen bekommen created_at/updated_at — Base erwartet sie an
jedem Modell, und genau das wurde bei geocode_cache vergessen (siehe
g8h2k4m7n1p5).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "k3m6p9r2t5w8"
down_revision = "j2k5n8q1r4t7"
branch_labels = None
depends_on = None


def _audit_spalten():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "website_visits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tag", sa.Date(), nullable=False),
        sa.Column("besucher_hash", sa.String(32), nullable=False),
        sa.Column("pfad", sa.String(120), nullable=False),
        sa.Column("ref_host", sa.String(120), nullable=True),
        sa.Column("art", sa.String(20), nullable=False),
        sa.Column("bot", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        *_audit_spalten(),
    )
    op.create_index("ix_website_visits_tag_art", "website_visits",
                    ["tag", "art"])
    op.create_index("ix_website_visits_tag_hash", "website_visits",
                    ["tag", "besucher_hash"])

    op.create_table(
        "website_tage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tag", sa.Date(), nullable=False),
        sa.Column("pfad", sa.String(120), nullable=False),
        sa.Column("ref_host", sa.String(120), nullable=False,
                  server_default=""),
        sa.Column("art", sa.String(20), nullable=False),
        sa.Column("aufrufe", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("besucher", sa.Integer(), nullable=False,
                  server_default="0"),
        *_audit_spalten(),
        sa.UniqueConstraint("tag", "pfad", "ref_host", "art",
                            name="uq_website_tage"),
    )
    op.create_index("ix_website_tage_tag", "website_tage", ["tag"])

    op.create_table(
        "website_salt",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tag", sa.Date(), nullable=False, unique=True),
        sa.Column("salt", sa.String(64), nullable=False),
        *_audit_spalten(),
    )
    op.create_index("ix_website_salt_tag", "website_salt", ["tag"],
                    unique=True)


def downgrade() -> None:
    op.drop_table("website_salt")
    op.drop_table("website_tage")
    op.drop_table("website_visits")
