"""werkstatt adresse + geocode cache

Revision ID: k4f1a8b2d6e3
Revises: j2c8e5f1a4d6
Create Date: 2026-05-10 09:30:00.000000

Erweitert tenants um die Werkstatt-Adresse des Handwerkers (Heimat-
Punkt fuer Tagesroute), und legt eine geocode_cache-Tabelle an, damit
Adresse-zu-Lat/Lon-Calls bei OpenRouteService nicht doppelt anfallen
(2.000 free-tier Requests pro Tag sind schnell weg, wenn jeder Slot-
Lookup neu geocoded).

Felder tenants:
- heimat_strasse, heimat_plz, heimat_ort: vom Tenant via /werkstatt
  Telegram-Wizard eingegeben; Anzeige-Form fuer den Tenant in /werkstatt
  status. PLZ getrennt fuer spaetere PLZ-Heuristik (gleiche PLZ-Region
  = klein, andere PLZ-Region = grosser Sprung) ohne dass wir geocoden
  muessen. Numeric(9,6) fuer lat/lon — damit deckt 6 Nachkommastellen
  ab Genauigkeit ~10cm, was lokal sehr fein ist.
- heimat_lat, heimat_lon: lat/lon-Koordinaten der Werkstatt
- fahrtzeit_puffer_min: Minuten die wir AUF die ORS-Fahrtzeit drauf
  rechnen (Material laden, Hände waschen, Kunde verabschieden).
  Default 15. Konfigurierbar pro Tenant (z.B. Sanitaer 25, Tischler 10).

Tabelle geocode_cache:
- address_key: SHA-256 der normalisierten Adresse (lower, trim,
  Doppel-Whitespace weg, Umlaute entfernt). Eindeutig.
- address_normalized: die normalisierte Eingabe (Debug-Wert)
- lat, lon: Geocoding-Ergebnis
- formatted: Formatierte Adresse von Pelias zum sanity-checken
- geocoded_at: wann gecached
- hit_count: wie oft schon gelesen (loeschen wir Eintraege mit hit=0
  und alter > 90 Tage spaeter, falls die Tabelle wuchert)

Mit normalisiertem Address-Key teilen Tenants den Cache (Adressen sind
nicht tenant-spezifisch). DSGVO-mässig OK weil nur Strasse/PLZ/Ort,
keine Personennamen.
"""
from alembic import op
import sqlalchemy as sa


revision = "k4f1a8b2d6e3"
down_revision = "j2c8e5f1a4d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- tenants erweitern ---
    op.add_column(
        "tenants",
        sa.Column("heimat_strasse", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("heimat_plz", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("heimat_ort", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("heimat_lat", sa.Numeric(precision=9, scale=6), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("heimat_lon", sa.Numeric(precision=9, scale=6), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "fahrtzeit_puffer_min",
            sa.Integer(),
            nullable=False,
            server_default="15",
        ),
    )

    # --- geocode_cache anlegen ---
    op.create_table(
        "geocode_cache",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("address_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("address_normalized", sa.String(length=500), nullable=False),
        sa.Column("lat", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("lon", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("formatted", sa.String(length=500), nullable=True),
        sa.Column(
            "geocoded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "hit_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    # address_key ist unique → impliziter Index. Trotzdem expliziter Index
    # fuer den Fall, dass wir spaeter LIKE-Queries auf address_normalized
    # machen wollen.
    op.create_index(
        "ix_geocode_cache_address_key",
        "geocode_cache",
        ["address_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_geocode_cache_address_key", table_name="geocode_cache")
    op.drop_table("geocode_cache")
    op.drop_column("tenants", "fahrtzeit_puffer_min")
    op.drop_column("tenants", "heimat_lon")
    op.drop_column("tenants", "heimat_lat")
    op.drop_column("tenants", "heimat_ort")
    op.drop_column("tenants", "heimat_plz")
    op.drop_column("tenants", "heimat_strasse")
