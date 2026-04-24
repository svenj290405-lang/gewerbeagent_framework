"""
Loescht einen Tenant komplett aus der DB (DSGVO: Right to be forgotten).

Nutzung:
  uv run python -m scripts.delete_tenant --slug testkunde
  uv run python -m scripts.delete_tenant --slug testkunde --force

Achtung: Die Operation ist UNWIDERRUFLICH.
Alle Daten des Tenants werden geloescht:
  - Tenant-Eintrag
  - Tool-Configs (Plugin-Aktivierungen + Konfiguration)
  - OAuth-Tokens (verschluesselte Google-Zugaenge)
  - Alle plugin-spezifischen Daten (via CASCADE, z.B. Termine in der Zukunft)

Was NICHT automatisch geloescht wird:
  - Google-Kalender-Eintraege beim Kunden (die gehoeren ihm)
  - sipgate-Rufnummern (muss manuell in sipgate storniert werden)
  - ElevenLabs-Agent-Config (muss manuell in ElevenLabs geloescht werden)
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import OAuthToken, Tenant, ToolConfig


async def delete_tenant(slug: str, force: bool = False) -> None:
    """Loescht einen Tenant und alle zugehoerigen Daten."""

    async with AsyncSessionLocal() as session:
        # Tenant holen + mit Relationships
        result = await session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )
        tenant = result.scalar_one_or_none()

        if not tenant:
            print(f"FEHLER: Kein Tenant mit Slug '{slug}' gefunden.")
            sys.exit(1)

        # Infos anzeigen vor Loeschung
        tool_count = len(tenant.tool_configs)
        oauth_count = len(tenant.oauth_tokens)

        print()
        print("=" * 70)
        print("  TENANT-LOESCHUNG")
        print("=" * 70)
        print(f"  Slug:              {tenant.slug}")
        print(f"  Firma:             {tenant.company_name}")
        print(f"  Kontakt:           {tenant.contact_name} <{tenant.contact_email}>")
        status_str = tenant.status.value if hasattr(tenant.status, 'value') else tenant.status
        print(f"  Status:            {status_str}")
        print(f"  Aktive Plugins:    {tool_count}")
        print(f"  OAuth-Tokens:      {oauth_count}")
        print(f"  Angelegt am:       {tenant.created_at.strftime('%d.%m.%Y %H:%M')}")
        print("=" * 70)
        print()

        # Sicherheits-Bestaetigung (falls nicht --force)
        if not force:
            print("ACHTUNG: Diese Operation ist UNWIDERRUFLICH.")
            print("Alle Daten des Tenants werden aus der Datenbank geloescht.")
            print()
            print("Was NICHT geloescht wird (manuell erledigen):")
            print("  - Google-Kalender-Eintraege beim Kunden")
            print("  - sipgate-Rufnummer (manuell in sipgate stornieren)")
            print("  - ElevenLabs-Agent-Config (manuell in ElevenLabs loeschen)")
            print()
            confirmation = input(f"Tenant '{slug}' wirklich loeschen? (yes/no): ").strip().lower()

            if confirmation not in ("yes", "y", "ja", "j"):
                print("Abgebrochen, keine Aenderungen gemacht.")
                sys.exit(0)

        # Loeschung durchfuehren
        # Dank CASCADE in den Fremdschluesseln werden tool_configs und
        # oauth_tokens automatisch mit geloescht.
        tenant_id = tenant.id
        await session.delete(tenant)
        await session.commit()

        print()
        print(f"Tenant '{slug}' erfolgreich geloescht.")
        print(f"  - {tool_count} Tool-Config(s) geloescht")
        print(f"  - {oauth_count} OAuth-Token(s) geloescht")
        print(f"  - Tenant-ID: {tenant_id}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Loescht einen Tenant komplett (DSGVO-konform).",
    )
    parser.add_argument("--slug", required=True, help="Slug des zu loeschenden Tenants")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Keine Bestaetigung abfragen (fuer Scripting). VORSICHT!",
    )
    args = parser.parse_args()

    asyncio.run(delete_tenant(slug=args.slug, force=args.force))


if __name__ == "__main__":
    main()
