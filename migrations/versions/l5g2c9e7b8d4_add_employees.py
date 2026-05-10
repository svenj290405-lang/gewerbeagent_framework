"""employees-Tabelle + Default-Employee-Backfill

Revision ID: l5g2c9e7b8d4
Revises: k4f1a8b2d6e3
Create Date: 2026-05-10 14:30:00.000000

Phase 0 der Multi-Mitarbeiter-Erweiterung
(Plan: das-machen-wir-gleich-foamy-frost.md).

Legt die employees-Tabelle an und befuellt sie mit einem Default-
Employee pro existierendem Tenant. Der Default-Employee erbt die
heutigen Tenant-Felder (contact_*, telegram_chat_id, heimat_*,
fahrtzeit_puffer_min) — damit ist der Code ab sofort employee-
zentrisch ohne Sonderfaelle.

Felder fuer alle 5 Phasen sind direkt vorgesehen:
- Phase 0: id, tenant_id, slug, name, contact_email, is_default,
  is_active, notes
- Phase 2: telegram_chat_id BigInt UNIQUE NULL
- Phase 3: heimat_strasse/plz/ort/lat/lon, fahrtzeit_puffer_min
- Phase 4: skills ARRAY(String), arbeitszeiten JSONB, arbeitstage
  ARRAY(Integer)

Constraints:
- Unique (tenant_id, slug) — kein Duplikat-Slug pro Tenant
- Partial-Unique-Index (tenant_id) WHERE is_default — exakt 1
  Default pro Tenant, durch Postgres garantiert
- Unique telegram_chat_id (global) — eine Chat-ID kann nur einem
  Mitarbeiter gehoeren

Backward-Compat: tenants.telegram_chat_id, tenants.heimat_* werden
NICHT gedroppt. Sie spiegeln den Default-Employee weiterhin, bis
in spaeteren Phasen alle Code-Pfade migriert sind. Cleanup waere
eine separate Migration in Monaten, nicht jetzt.

Backfill ist trivial weil aktuell nur 2 Tenants in der DB sind.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID


revision = "l5g2c9e7b8d4"
down_revision = "k4f1a8b2d6e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- employees anlegen ---
    op.create_table(
        "employees",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Identitaet
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("contact_email", sa.String(length=200), nullable=True),
        sa.Column(
            "is_default", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
        sa.Column(
            "is_active", sa.Boolean(),
            nullable=False, server_default=sa.text("true"),
        ),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        # Phase 2 — Telegram
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        # Phase 3 — Heimat-Geo
        sa.Column("heimat_strasse", sa.String(length=255), nullable=True),
        sa.Column("heimat_plz", sa.String(length=10), nullable=True),
        sa.Column("heimat_ort", sa.String(length=200), nullable=True),
        sa.Column("heimat_lat", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("heimat_lon", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column(
            "fahrtzeit_puffer_min", sa.Integer(),
            nullable=False, server_default="15",
        ),
        # Phase 4 — Skills + Arbeitszeiten
        sa.Column("skills", ARRAY(sa.String(length=50)), nullable=True),
        sa.Column("arbeitszeiten", JSONB(), nullable=True),
        sa.Column("arbeitstage", ARRAY(sa.Integer()), nullable=True),
        # Base-Felder
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    # --- Indizes / Constraints ---
    op.create_unique_constraint(
        "uq_emp_tenant_slug", "employees", ["tenant_id", "slug"],
    )
    op.create_index(
        "ix_employees_tenant_id", "employees", ["tenant_id"],
    )
    op.create_index(
        "ix_emp_tenant_default", "employees", ["tenant_id", "is_default"],
    )
    op.create_index(
        "ix_emp_chat", "employees", ["telegram_chat_id"],
    )
    # Partial-Unique: max 1 Default pro Tenant. Postgres-Feature.
    op.create_index(
        "uq_emp_default_per_tenant", "employees", ["tenant_id"],
        unique=True, postgresql_where=sa.text("is_default"),
    )
    # Globale Eindeutigkeit der Chat-ID (eine Chat = 1 Employee).
    # Wird ueber unique-constraint ausgedrueckt, NULL erlaubt mehrfach.
    op.create_unique_constraint(
        "uq_emp_telegram_chat_id", "employees", ["telegram_chat_id"],
    )

    # --- Backfill: 1 Default-Employee pro existierendem Tenant ---
    # Slug "default", Name aus tenant.contact_name, Kontakt-/Heimat-/
    # Telegram-Felder gespiegelt. Falls schon ein Employee mit slug
    # "default" existiert (manueller Vorlauf), nicht doppelt anlegen.
    conn = op.get_bind()
    conn.execute(sa.text("""
        INSERT INTO employees
            (id, tenant_id, slug, name, contact_email, is_default, is_active,
             telegram_chat_id, heimat_strasse, heimat_plz, heimat_ort,
             heimat_lat, heimat_lon, fahrtzeit_puffer_min,
             created_at, updated_at)
        SELECT
            gen_random_uuid(), t.id, 'default', t.contact_name, t.contact_email,
            true, true,
            t.telegram_chat_id,
            t.heimat_strasse, t.heimat_plz, t.heimat_ort,
            t.heimat_lat, t.heimat_lon, t.fahrtzeit_puffer_min,
            now(), now()
        FROM tenants t
        WHERE NOT EXISTS (
            SELECT 1 FROM employees e
            WHERE e.tenant_id = t.id AND e.slug = 'default'
        )
    """))


def downgrade() -> None:
    op.drop_constraint(
        "uq_emp_telegram_chat_id", "employees", type_="unique",
    )
    op.drop_index("uq_emp_default_per_tenant", table_name="employees")
    op.drop_index("ix_emp_chat", table_name="employees")
    op.drop_index("ix_emp_tenant_default", table_name="employees")
    op.drop_index("ix_employees_tenant_id", table_name="employees")
    op.drop_constraint("uq_emp_tenant_slug", "employees", type_="unique")
    op.drop_table("employees")
