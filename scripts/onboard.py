"""
Onboarding-Script fuer neue Tenants (Kunden).

Nutzung:
  uv run python -m scripts.onboard --slug dietz --name "Tischlerei Dietz" \
      --email "f.dietz@pura-tischler.de" --contact "Fabian Dietz" \
      --phone "+49 6502 12345" --tier pro

Das Script:
1. Legt Tenant in DB an (Status: ONBOARDING)
2. Setzt das gewaehlte Paket (basis | pro | enterprise) — aktiviert
   alle Features im Paket via apply_package()
3. Konfiguriert die Kalender-Defaults (arbeitszeiten, etc.)
4. Generiert Google-OAuth-URL fuer Kalender-Verknuepfung
5. Gibt eine Checkliste aus mit Rest-Schritten

Wenn --tier nicht angegeben wird, fragt das Skript interaktiv.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.features import apply_package
from core.features.catalog import (
    PACKAGE_BASIS, PACKAGE_PRO, PACKAGE_ENTERPRISE, ALL_PACKAGES,
    PACKAGE_LABELS, PACKAGES, FEATURES,
)
from core.models import Tenant, TenantStatus, ToolConfig
from core.security.oauth_flow import generate_auth_url
from config.settings import settings


DEFAULT_KALENDER_CONFIG = {
    "betrieb_name": "",              # wird aus Tenant.company_name uebernommen
    "calendar_id": "primary",
    "arbeitszeiten_start": "08:00",
    "arbeitszeiten_ende": "17:00",
    "arbeitstage": [0, 1, 2, 3, 4],  # Mo-Fr
    "termin_dauer_minuten": 90,
    "zeitzone": "Europe/Berlin",
}


def _prompt_tier() -> str:
    """Interaktiver Paket-Prompt wenn --tier nicht uebergeben."""
    print()
    print("Welches Paket soll der Tenant bekommen?")
    print()
    pkg_descriptions = {
        PACKAGE_BASIS: "Telegram + Kalender + Wissensbasis",
        PACKAGE_PRO: "+ Mail + Anfrage-Form + Lexware + Material + Kalkulation + Werkstatt",
        PACKAGE_ENTERPRISE: "+ Voice + Drive-Archiv + Visualisierung + Mitarbeiter",
    }
    options = [PACKAGE_BASIS, PACKAGE_PRO, PACKAGE_ENTERPRISE]
    for i, pkg in enumerate(options, 1):
        feature_count = len(PACKAGES[pkg])
        print(f"  [{i}] {PACKAGE_LABELS[pkg]:18} ({feature_count} Features)")
        print(f"      {pkg_descriptions[pkg]}")
    print()
    while True:
        choice = input("Wahl [1-3, default=2]: ").strip() or "2"
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print("Ungueltige Wahl. Bitte 1, 2 oder 3.")


async def onboard_tenant(
    slug: str,
    name: str,
    email: str,
    contact: str,
    tier: str,
    phone: str | None = None,
    notes: str | None = None,
) -> None:
    """Legt einen neuen Tenant an und konfiguriert ihn."""

    # Validierung: Slug-Format
    if not slug.isalnum() and not all(c.isalnum() or c in "-_" for c in slug):
        print(f"FEHLER: Slug '{slug}' darf nur Buchstaben, Zahlen, - und _ enthalten.")
        sys.exit(1)
    if len(slug) < 2 or len(slug) > 50:
        print(f"FEHLER: Slug muss zwischen 2-50 Zeichen lang sein.")
        sys.exit(1)

    # Validierung: tier
    if tier not in ALL_PACKAGES:
        print(f"FEHLER: Unbekanntes Paket '{tier}'. Erlaubt: {', '.join(ALL_PACKAGES)}")
        sys.exit(1)
    if tier == "custom":
        print(
            "FEHLER: Onboarding mit 'custom'-Tier ist nicht sinnvoll — "
            "starte mit basis/pro/enterprise und togglen einzelne "
            "Features hinterher via Admin-UI.",
        )
        sys.exit(1)

    async with AsyncSessionLocal() as session:
        # Pruefen ob Slug schon existiert
        existing = await session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )
        if existing.scalar_one_or_none():
            print(f"FEHLER: Tenant mit Slug '{slug}' existiert bereits.")
            print(f"Waehle einen anderen Slug oder loesche den bestehenden Tenant.")
            sys.exit(1)

        # Tenant anlegen
        tenant = Tenant(
            slug=slug,
            company_name=name,
            contact_name=contact,
            contact_email=email,
            contact_phone=phone,
            status=TenantStatus.ONBOARDING,
            notes=notes,
            package_tier=tier,
        )
        session.add(tenant)
        await session.flush()  # damit tenant.id verfuegbar ist
        tenant_id = tenant.id

        # Kalender-Plugin extra konfigurieren (Defaults). apply_package
        # setzt enabled=True wenn 'kalender' im Paket — wir ergaenzen
        # nur die config-Daten.
        kalender_config = dict(DEFAULT_KALENDER_CONFIG)
        kalender_config["betrieb_name"] = name

        tool_config = ToolConfig(
            tenant_id=tenant_id,
            tool_name="kalender",
            enabled=True,
            config=kalender_config,
        )
        session.add(tool_config)

        await session.commit()

    # Paket anwenden (eigene Session intern in apply_package).
    # Setzt ToolConfig.enabled fuer alle Catalog-Features entsprechend
    # dem Paket. Bestehende ToolConfig.config (inkl. der gerade gesetzten
    # Kalender-Defaults) bleibt erhalten — apply_package aendert nur das
    # enabled-Flag.
    await apply_package(tenant_id, tier)

    # OAuth-URL generieren (ausserhalb der Session, braucht keinen DB-Zugriff)
    oauth_url = await generate_auth_url(tenant_slug=slug, provider="google")

    # Feature-Liste fuer Output zusammenstellen
    enabled_in_tier = sorted(PACKAGES[tier])
    feature_labels = [
        FEATURES[k].label for k in enabled_in_tier if k in FEATURES
    ]

    # Checkliste ausgeben
    print()
    print("=" * 70)
    print(f"  TENANT ANGELEGT: {name}")
    print("=" * 70)
    print(f"  Slug:          {slug}")
    print(f"  ID:            {tenant_id}")
    print(f"  Status:        ONBOARDING")
    print(f"  Paket:         {PACKAGE_LABELS.get(tier, tier)} ({len(enabled_in_tier)} Features)")
    print(f"  Kalender:      Mo-Fr 08:00-17:00, 90 Min Standard")
    print()
    print(f"  AKTIVIERTE FEATURES:")
    for label in feature_labels:
        print(f"    ✓ {label}")
    print()
    print("  NAECHSTE SCHRITTE:")
    print()
    print("  1. Google-Kalender verknuepfen")
    print("     Sende folgenden Link an den Kunden (oder klicke selbst,")
    print("     falls du mit einem eigenen Test-Account arbeitest):")
    print()
    print(f"     {oauth_url}")
    print()
    print("  2. sipgate-Nummer buchen (manuell)")
    print("     a) Einloggen bei sipgate: https://app.sipgate.com")
    print("     b) Neue Ortsnetzrufnummer buchen (passend zur Kunden-Vorwahl)")
    print("     c) Nummer im Framework verknuepfen:")
    print(f"        uv run python -m scripts.assign_number --slug {slug} --number 06xx-xxxx")
    print()
    print("  3. ElevenLabs-Agent konfigurieren")
    print("     a) Agent 'Q' klonen im ElevenLabs-Dashboard")
    print("     b) Prompt anpassen (Betrieb, Gewerk, Begruessung)")
    print("     c) Webhook-URLs setzen auf:")
    print(f"        POST {settings.public_url}/webhook/{slug}/kalender/check_availability")
    print(f"        POST {settings.public_url}/webhook/{slug}/kalender/book_appointment")
    print("     d) sipgate-Nummer mit ElevenLabs-Agent verknuepfen")
    print()
    print("  4. Rufumleitung einrichten lassen beim Kunden")
    print("     GSM-Code: **21*<SIPGATE_NUMMER>#")
    print("     Oder Anleitung fuer seinen Provider schicken")
    print()
    print("  5. Test-Anruf durchfuehren und dann:")
    print(f"     uv run python -m scripts.activate_tenant --slug {slug}")
    print("     (setzt Status auf ACTIVE)")
    print()
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Legt einen neuen Tenant (Kunden) im Framework an.",
    )
    parser.add_argument("--slug", required=True, help="Kurz-Identifier, z.B. 'dietz'")
    parser.add_argument("--name", required=True, help="Firmenname")
    parser.add_argument("--email", required=True, help="Kontakt-E-Mail")
    parser.add_argument("--contact", required=True, help="Ansprechpartner-Name")
    parser.add_argument("--phone", default=None, help="Telefon (optional)")
    parser.add_argument("--notes", default=None, help="Interne Notizen (optional)")
    parser.add_argument(
        "--tier", default=None,
        choices=[PACKAGE_BASIS, PACKAGE_PRO, PACKAGE_ENTERPRISE],
        help="Paket-Tier (basis/pro/enterprise). Wenn nicht gesetzt, wird interaktiv gefragt.",
    )
    args = parser.parse_args()

    tier = args.tier
    if tier is None:
        tier = _prompt_tier()

    asyncio.run(
        onboard_tenant(
            slug=args.slug,
            name=args.name,
            email=args.email,
            contact=args.contact,
            phone=args.phone,
            notes=args.notes,
            tier=tier,
        )
    )


if __name__ == "__main__":
    main()
