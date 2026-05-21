"""
Onboarding-Script fuer neue Tenants (Kunden).

Nutzung:
  uv run python -m scripts.onboard --slug dietz --name "Tischlerei Dietz" \
      --email "f.dietz@pura-tischler.de" --contact "Fabian Dietz" \
      --phone "+49 6502 12345"

Das Script:
1. Legt Tenant + Default-Employee (Inhaber, slug 'default') in DB an
   (Status: ONBOARDING). Der Default-Employee ist Pflicht — der
   /start <slug>-Onboarding-Link bindet den Telegram-Chat an ihn.
2. Aktiviert das Default-Feature-Set (Kalender, Wissensbasis, Mail,
   Anfrage-Formular, Lexware, Material). Alles Weitere (Voice, Drive,
   Visualisierung, Mitarbeiter) schaltet Sven per Admin-UI dazu.
3. Konfiguriert die Kalender-Defaults (arbeitszeiten, etc.)
4. Generiert Google-OAuth-URL fuer Kalender-Verknuepfung
5. Gibt eine Checkliste aus mit Rest-Schritten
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.features.catalog import FEATURES
from core.features.check import GLOBALLY_DISABLED_FEATURES
from core.models import (
    Tenant, TenantKnowledge, TenantStatus, ToolConfig,
    ALLE_KATEGORIEN,
)
from core.models.employee import Employee
from core.security.oauth_flow import generate_auth_url
from config.settings import settings


# Branchen-Templates Phase B10
_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load_branche_template(branche: str | None) -> dict | None:
    """Liest scripts/templates/branche_<key>.json. None wenn nicht vorhanden."""
    if not branche:
        return None
    path = _TEMPLATES_DIR / f"branche_{branche}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: branche-Template {path.name} unleserlich: {e}")
        return None


def _list_available_branches() -> list[str]:
    """Liefert alle vorhandenen Branche-Keys."""
    return sorted(
        p.stem.removeprefix("branche_")
        for p in _TEMPLATES_DIR.glob("branche_*.json")
    )


async def _load_global_bot_token() -> str | None:
    """Holt den zentralen Telegram-Bot-Token aus _global ToolConfig.

    Identisches Pattern wie scripts/generate_qr.py:60-75 und
    plugins/telegram_notify/handler.py:_load_global_bot_token. Bewusst
    dupliziert damit das Skript ohne Plugin-Imports auskommt.
    """
    from core.models import Tenant as _T, ToolConfig as _TC
    async with AsyncSessionLocal() as s:
        gt = (await s.execute(
            select(_T).where(_T.slug == "_global")
        )).scalar_one_or_none()
        if not gt:
            return None
        tc = (await s.execute(
            select(_TC).where(
                _TC.tenant_id == gt.id, _TC.tool_name == "telegram_bot",
            )
        )).scalar_one_or_none()
        if not tc or not tc.enabled:
            return None
        return (tc.config or {}).get("bot_token") or None


async def _ensure_telegram_webhook() -> tuple[bool, str]:
    """Stellt sicher dass der zentrale Telegram-Webhook auf _global zeigt.

    Architektur: EIN Bot, EIN Webhook. Der Plugin-Handler dispatched
    intern via chat_id-Lookup zum richtigen Tenant — der URL-Pfad
    selber ist deshalb konstant `/webhook/_global/telegram_notify/incoming`.
    Pro Onboarding rufen wir es idempotent auf — falls Sven mal den
    Webhook anderswo hingebogen hatte (z.B. dev-Test).

    Returns (success, info). Failsafe.
    """
    bot_token = await _load_global_bot_token()
    if not bot_token:
        return False, "Bot-Token in _global telegram_bot fehlt"

    public_url = settings.public_url.rstrip("/")
    webhook_url = f"{public_url}/webhook/_global/telegram_notify/incoming"
    secret = settings.telegram_webhook_secret or ""

    payload = {"url": webhook_url}
    if secret:
        payload["secret_token"] = secret

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Vorher pruefen ob der Webhook schon stimmt — vermeidet
            # unnoetigen Telegram-API-Call bei jedem onboard-Lauf.
            info_resp = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getWebhookInfo",
            )
            info_data = info_resp.json() if info_resp.content else {}
            current_url = (info_data.get("result") or {}).get("url", "")
            if current_url == webhook_url:
                return True, f"{webhook_url} (schon korrekt)"

            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/setWebhook",
                json=payload,
            )
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and data.get("ok"):
            return True, webhook_url
        return False, f"HTTP {resp.status_code}: {data.get('description', resp.text[:120])}"
    except Exception as e:
        return False, f"Exception: {e}"


# Feature-Set das jeder neue Tenant per Default bekommt. Es gibt keine
# Pakete/Tiers mehr — alles Weitere (Voice, Drive-Archiv, Visualisierung,
# Mitarbeiter) wird nach dem Onboarding per Admin-UI einzeln dazugeschaltet.
# Always-on-Features (telegram_bot, kunde_lookup) sind ohnehin immer aktiv.
# Standardmaessig werden ALLE umschaltbaren Tools aktiviert. Ausgenommen:
# - always-on-Features (telegram_bot, kunde_lookup) — brauchen keinen Toggle
# - global per Kill-Switch deaktivierte (z.B. werkstatt)
# Dynamisch aus dem Katalog, damit kuenftige Features automatisch dabei sind.
DEFAULT_FEATURES: tuple[str, ...] = tuple(
    key for key, feat in FEATURES.items()
    if not getattr(feat, "always_on", False)
    and key not in GLOBALLY_DISABLED_FEATURES
)


DEFAULT_KALENDER_CONFIG = {
    "betrieb_name": "",              # wird aus Tenant.company_name uebernommen
    "calendar_id": "primary",
    "arbeitszeiten_start": "08:00",
    "arbeitszeiten_ende": "17:00",
    "arbeitstage": [0, 1, 2, 3, 4],  # Mo-Fr
    "termin_dauer_minuten": 90,
    "zeitzone": "Europe/Berlin",
}


async def onboard_tenant(
    slug: str,
    name: str,
    email: str,
    contact: str,
    phone: str | None = None,
    notes: str | None = None,
    branche: str | None = None,
) -> None:
    """Legt einen neuen Tenant an und konfiguriert ihn.

    branche (Phase B10): wenn gesetzt, wird das passende Template aus
    scripts/templates/branche_<branche>.json geladen und als
    TenantKnowledge-Eintraege geschrieben. Tenant kann sie nachher
    via /wissen anpassen. Wenn das Template nicht existiert, wird die
    Wissensbasis leer angelegt (heute-Default).
    """

    # Validierung: Slug-Format
    if not slug.isalnum() and not all(c.isalnum() or c in "-_" for c in slug):
        print(f"FEHLER: Slug '{slug}' darf nur Buchstaben, Zahlen, - und _ enthalten.")
        sys.exit(1)
    if len(slug) < 2 or len(slug) > 50:
        print(f"FEHLER: Slug muss zwischen 2-50 Zeichen lang sein.")
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
            branche=branche,
        )
        session.add(tenant)
        await session.flush()  # damit tenant.id verfuegbar ist
        tenant_id = tenant.id

        # Default-Employee (Inhaber) — slug 'default', is_default=True.
        # Pflicht: der /start <slug>-Onboarding-Deep-Link bindet den
        # Telegram-Chat an genau diesen Employee. Ohne ihn schlaegt
        # /start mit "Mitarbeiter-Slug 'default' nicht gefunden" fehl.
        # Heimat-/Telegram-/Kalender-Felder bleiben leer und werden im
        # Onboarding-Wizard bzw. via /kalender_verbinden gefuellt.
        session.add(Employee(
            tenant_id=tenant_id,
            slug="default",
            name=contact,
            contact_email=email,
            is_default=True,
        ))

        # Kalender-Plugin mit Defaults konfigurieren (config-Daten).
        kalender_config = dict(DEFAULT_KALENDER_CONFIG)
        kalender_config["betrieb_name"] = name

        session.add(ToolConfig(
            tenant_id=tenant_id,
            tool_name="kalender",
            enabled=True,
            config=kalender_config,
        ))

        # Restliche Default-Features als simple enabled-Flags anlegen.
        # kalender ist oben schon mit config dabei -> hier ueberspringen.
        for feat_key in DEFAULT_FEATURES:
            if feat_key == "kalender":
                continue
            session.add(ToolConfig(
                tenant_id=tenant_id,
                tool_name=feat_key,
                enabled=True,
                config={},
            ))

        await session.commit()

    # Phase B10: Branchen-Template laden + als TenantKnowledge schreiben
    knowledge_loaded = 0
    template = _load_branche_template(branche)
    if template is not None:
        try:
            async with AsyncSessionLocal() as session:
                for entry in template.get("knowledge", []):
                    kat = entry.get("kategorie", "")
                    text = entry.get("text", "")
                    if kat in ALLE_KATEGORIEN and text:
                        session.add(TenantKnowledge(
                            tenant_id=tenant_id,
                            kategorie=kat,
                            text=text[:2000],
                        ))
                        knowledge_loaded += 1
                await session.commit()
        except Exception as e:
            print(f"WARN: Template-Knowledge konnte nicht geladen werden: {e}")

    # Beta-1 B1-2: Telegram-Webhook absichern. Architektur ist EIN
    # zentraler Bot, EIN Webhook auf _global — der Handler dispatched
    # via chat_id-Lookup. Failsafe — Skript laeuft weiter, Sven sieht
    # im Output ob er manuell nachholen muss.
    webhook_ok, webhook_info = await _ensure_telegram_webhook()

    # Beta-1 B1-3: QR-Code direkt generieren. Failsafe — bei Fehler nur
    # Hinweis, kein Abbruch.
    qr_path: Path | None = None
    qr_link: str | None = None
    try:
        from scripts.generate_qr import generate_for_slug
        qr_result = await generate_for_slug(slug)
        qr_path = qr_result.png_path
        qr_link = qr_result.deep_link
    except Exception as e:
        print(f"WARN: QR-Code konnte nicht generiert werden: {e}")

    # OAuth-URL generieren (ausserhalb der Session, braucht keinen DB-Zugriff)
    oauth_url = await generate_auth_url(tenant_slug=slug, provider="google")

    # Feature-Liste fuer Output: Default-Set + always-on (immer aktiv)
    activated_keys = set(DEFAULT_FEATURES) | {
        k for k, f in FEATURES.items() if f.always_on
    }
    feature_labels = sorted(
        FEATURES[k].label for k in activated_keys if k in FEATURES
    )

    # Checkliste ausgeben
    print()
    print("=" * 70)
    print(f"  TENANT ANGELEGT: {name}")
    print("=" * 70)
    print(f"  Slug:          {slug}")
    print(f"  ID:            {tenant_id}")
    print(f"  Status:        ONBOARDING")
    print(f"  Features:      Default-Set ({len(feature_labels)} aktiv)")
    print(f"  Kalender:      Mo-Fr 08:00-17:00, 90 Min Standard")
    if branche:
        knowledge_msg = (
            f" + {knowledge_loaded} Wissens-Eintraege"
            if knowledge_loaded else " (kein Template gefunden)"
        )
        print(f"  Branche:       {branche}{knowledge_msg}")
    if webhook_ok:
        print(f"  Telegram-Webhook: ✓ registriert")
    else:
        print(f"  Telegram-Webhook: ✗ {webhook_info}")
    if qr_path:
        print(f"  QR-Code:       {qr_path}")
        print(f"  Deep-Link:     {qr_link}")
    print()
    print(f"  AKTIVIERTE FEATURES:")
    for label in feature_labels:
        print(f"    ✓ {label}")
    print()
    print("  NAECHSTE SCHRITTE:")
    print()
    print("  0. AVV-Template ausfuellen + per Mail an Kunde schicken")
    print(f"     Template: LEGAL/AVV-Template.md  (Platzhalter wie")
    print(f"     {{{{TENANT_COMPANY}}}} ersetzen)")
    print(f"     Subprozessoren-Liste: LEGAL/Subprozessoren-Liste.md")
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
    available_branches = _list_available_branches()
    parser.add_argument(
        "--branche", default=None,
        choices=available_branches if available_branches else None,
        help=(
            f"Branchen-Template fuer Wissensbasis-Defaults. "
            f"Verfuegbar: {', '.join(available_branches) or '(keine Templates)'}. "
            f"Ohne dieses Flag startet der Tenant mit leerer Wissensbasis."
        ),
    )
    args = parser.parse_args()

    asyncio.run(
        onboard_tenant(
            slug=args.slug,
            name=args.name,
            email=args.email,
            contact=args.contact,
            phone=args.phone,
            notes=args.notes,
            branche=args.branche,
        )
    )


if __name__ == "__main__":
    main()
