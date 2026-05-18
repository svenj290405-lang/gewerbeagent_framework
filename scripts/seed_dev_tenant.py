"""Seed-Skript fuer den Dev-Stack.

Legt im Dev-DB einen Demo-Tenant 'sven-dev' an inkl. Default-Employee.
Idempotent — bei mehrfachem Aufruf wird der bestehende Tenant nur
nachjustiert, kein Duplikat erstellt.

Verwendung (im Dev-Container):
    docker compose -p dev exec framework_dev \
        uv run python -m scripts.seed_dev_tenant

Voraussetzungen:
- DATABASE_URL zeigt auf gewerbeagent_dev (NICHT auf prod-DB)
- alembic upgrade head ist gelaufen

Sicherheits-Check: das Skript bricht ab wenn settings.is_production=True,
damit ein Fehlkonfiguration nicht versehentlich Demo-Daten in die
echte Prod-DB schreibt.
"""
from __future__ import annotations

import asyncio
import sys
import uuid

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, TenantStatus, ToolConfig
from core.models.employee import Employee, ALLE_SKILLS
from config.settings import settings


DEV_TENANT_SLUG = "sven-dev"
DEV_TENANT_COMPANY = "Sven Dev-Tenant"
DEV_TENANT_EMAIL = "dev@gewerbeagent.local"
DEV_TENANT_CONTACT = "Sven (Dev)"


async def seed() -> None:
    if settings.is_production:
        print("FEHLER: settings.is_production=True. Seed nur im Dev erlaubt.")
        print(f"Aktuelle DATABASE_URL: {settings.database_url}")
        sys.exit(2)

    if "_dev" not in settings.database_url and "localhost" not in settings.database_url:
        print("WARNUNG: DATABASE_URL enthaelt weder '_dev' noch 'localhost'.")
        print(f"Aktuelle DATABASE_URL: {settings.database_url}")
        print("Sicher dass das die Dev-DB ist? [yes/no]: ", end="")
        if input().strip().lower() != "yes":
            print("Abgebrochen.")
            sys.exit(1)

    async with AsyncSessionLocal() as session:
        # 1. Tenant
        existing = (await session.execute(
            select(Tenant).where(Tenant.slug == DEV_TENANT_SLUG)
        )).scalar_one_or_none()

        if existing:
            print(f"✓ Tenant '{DEV_TENANT_SLUG}' existiert (id={existing.id})")
            tenant = existing
        else:
            tenant = Tenant(
                slug=DEV_TENANT_SLUG,
                company_name=DEV_TENANT_COMPANY,
                contact_name=DEV_TENANT_CONTACT,
                contact_email=DEV_TENANT_EMAIL,
                status=TenantStatus.ACTIVE,  # Direkt aktiv, keine Onboarding-Schritte
                branche="allgemein",
                notes="Dev-Stack Demo-Tenant. Nicht in Produktion verwenden.",
            )
            session.add(tenant)
            await session.flush()
            print(f"+ Tenant '{DEV_TENANT_SLUG}' angelegt (id={tenant.id})")

        # 2. Default-Employee (Inhaber)
        existing_emp = (await session.execute(
            select(Employee)
            .where(Employee.tenant_id == tenant.id)
            .where(Employee.is_default.is_(True))
        )).scalar_one_or_none()

        if existing_emp:
            print(f"✓ Default-Employee existiert ({existing_emp.name})")
        else:
            emp = Employee(
                tenant_id=tenant.id,
                slug="sven",
                name="Sven (Dev)",
                is_default=True,
                skills=list(ALLE_SKILLS),  # alle Gewerke fuer Test-Routing
            )
            session.add(emp)
            print(f"+ Default-Employee 'sven' angelegt")

        # 3. Tool-Configs (alle Plugins enabled fuer Dev-Tests)
        # Wir aktivieren alles damit Sven jedes Feature lokal testen kann.
        # Im Prod-Onboarding entscheidet das die Paket-Auswahl.
        tools_to_enable = [
            "telegram_bot",
            "telegram_notify",
            "kalender",
            "lexware",
            "voice_init",
        ]
        for tool_name in tools_to_enable:
            tc = (await session.execute(
                select(ToolConfig)
                .where(ToolConfig.tenant_id == tenant.id)
                .where(ToolConfig.tool_name == tool_name)
            )).scalar_one_or_none()
            if tc:
                if not tc.enabled:
                    tc.enabled = True
                    print(f"  ToolConfig '{tool_name}' aktiviert")
                else:
                    print(f"  ToolConfig '{tool_name}' bereits aktiv")
            else:
                session.add(ToolConfig(
                    tenant_id=tenant.id,
                    tool_name=tool_name,
                    enabled=True,
                    config={},
                ))
                print(f"+ ToolConfig '{tool_name}' angelegt")

        await session.commit()

    print()
    print("=" * 60)
    print(f"  DEV-TENANT BEREIT: {DEV_TENANT_SLUG}")
    print("=" * 60)
    print(f"  Slug:        {DEV_TENANT_SLUG}")
    print(f"  Status:      ACTIVE")
    print(f"  Public-URL:  {settings.public_url}")
    print()
    print("  NAECHSTE SCHRITTE (manuell):")
    print(f"  1. Telegram-Bot @ Q_dev_bot starten (siehe INFRA-MANUAL-STEPS.md)")
    print(f"  2. /start sven-dev im Dev-Bot → Telegram-Chat verknuepfen")
    print(f"  3. /kalender_verbinden → OAuth via {settings.public_url}/oauth/callback")
    print(f"  4. /help → testen")
    print()
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(seed())
