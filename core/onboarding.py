"""Wiederverwendbare Onboarding-Logik fuer neue Tenants (Kunden).

Frueher lag die komplette Tenant-Erstellung im CLI-Skript
`scripts/onboard.py` (mit `print`-Checkliste + `sys.exit`). Damit die
Admin-UI denselben Weg nutzen kann, ist die reine Logik hier
extrahiert: `create_tenant_record` legt den Tenant + Default-Employee +
Default-Features an (wirft `ValueError` statt `sys.exit`, keine Prints),
und `build_owner_activation_link` erzeugt den **sicheren** Inhaber-
Onboarding-Link.

Sicherheit (S13): Der Inhaber wird NICHT mehr ueber einen ratbaren
`/start <slug>` gebunden, sondern ueber einen einmaligen, zufaelligen
Aktivierungs-Token — exakt derselbe Mechanismus wie beim Mitarbeiter-
Onboarding (`?start=activate_<token>`). Der Token steckt im Link, der
dem Kunden per Mail zugeschickt wird.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.features.catalog import FEATURES
from core.features.check import GLOBALLY_DISABLED_FEATURES
from core.models import (
    ALLE_KATEGORIEN,
    Tenant,
    TenantKnowledge,
    TenantStatus,
    ToolConfig,
    create_activation_token,
)
from core.models.employee import Employee

logger = logging.getLogger(__name__)

GLOBAL_TENANT_SLUG = "_global"

# Branchen-Templates liegen weiterhin unter scripts/templates/ (reines
# Daten-Verzeichnis, unveraendert). core/onboarding.py liegt eine Ebene
# unter dem Repo-Root.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "scripts" / "templates"

# Feature-Set das jeder neue Tenant per Default bekommt — dynamisch aus
# dem Katalog, ausgenommen always-on (telegram_bot, kunde_lookup) und
# global per Kill-Switch deaktivierte (z.B. werkstatt). Deckungsgleich
# mit der alten Konstante in scripts/onboard.py.
DEFAULT_FEATURES: tuple[str, ...] = tuple(
    key for key, feat in FEATURES.items()
    if not getattr(feat, "always_on", False)
    and key not in GLOBALLY_DISABLED_FEATURES
)

DEFAULT_KALENDER_CONFIG = {
    "betrieb_name": "",              # wird aus name uebernommen
    "calendar_id": "primary",
    "arbeitszeiten_start": "08:00",
    "arbeitszeiten_ende": "17:00",
    "arbeitstage": [0, 1, 2, 3, 4],  # Mo-Fr
    "termin_dauer_minuten": 90,
    "zeitzone": "Europe/Berlin",
}

# Inhaber-Onboarding-Token: laenger gueltig als der Mitarbeiter-Default
# (7 Tage), weil ein Betriebsinhaber nach dem Erst-Mail-Versand auch mal
# ein paar Tage braucht, bis er klickt.
OWNER_TOKEN_TTL_DAYS = 14


class OnboardingError(ValueError):
    """Fachlicher Onboarding-Fehler (z.B. Slug ungueltig/vergeben).

    Erbt von ValueError, damit bestehende Aufrufer, die nur ValueError
    fangen, weiter funktionieren — aber die Admin-Route kann gezielt
    darauf reagieren und die Meldung dem Nutzer zeigen.
    """


@dataclass
class ProvisionResult:
    """Ergebnis von create_tenant_record."""
    tenant_id: uuid.UUID
    default_employee_id: uuid.UUID
    activated_features: list[str] = field(default_factory=list)
    knowledge_loaded: int = 0


# ---------------------------------------------------------------------
# Branchen-Templates
# ---------------------------------------------------------------------

def load_branche_template(branche: str | None) -> dict | None:
    """Liest scripts/templates/branche_<key>.json. None wenn nicht da."""
    if not branche:
        return None
    path = _TEMPLATES_DIR / f"branche_{branche}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("branche-Template %s unleserlich: %s", path.name, e)
        return None


def list_available_branches() -> list[str]:
    """Liefert alle vorhandenen Branche-Keys (fuer CLI-choices + UI)."""
    if not _TEMPLATES_DIR.exists():
        return []
    return sorted(
        p.stem.removeprefix("branche_")
        for p in _TEMPLATES_DIR.glob("branche_*.json")
    )


def _validate_slug(slug: str) -> None:
    """Wirft OnboardingError wenn der Slug das Format verletzt."""
    if not all(c.isalnum() or c in "-_" for c in slug):
        raise OnboardingError(
            f"Slug '{slug}' darf nur Buchstaben, Zahlen, - und _ enthalten."
        )
    if len(slug) < 2 or len(slug) > 50:
        raise OnboardingError("Slug muss zwischen 2 und 50 Zeichen lang sein.")
    if slug == GLOBAL_TENANT_SLUG:
        raise OnboardingError("'_global' ist reserviert.")


# ---------------------------------------------------------------------
# Tenant-Erstellung (DB) — gemeinsam fuer CLI + Admin-UI
# ---------------------------------------------------------------------

async def create_tenant_record(
    *,
    slug: str,
    name: str,
    email: str,
    contact: str,
    phone: str | None = None,
    notes: str | None = None,
    branche: str | None = None,
) -> ProvisionResult:
    """Legt einen neuen Tenant + Default-Employee + Default-Features an.

    Wirft OnboardingError (ValueError) bei Validierungs-/Konfliktfehlern
    statt das Programm zu beenden — damit aus einem Web-Request heraus
    nutzbar. Seedet bei gesetzter Branche die Wissensbasis aus dem
    Template. Gibt die IDs + eine Liste der aktivierten Features zurueck.
    """
    slug = (slug or "").strip().lower()
    name = (name or "").strip()
    email = (email or "").strip()
    contact = (contact or "").strip()
    _validate_slug(slug)
    if not name:
        raise OnboardingError("Firmenname fehlt.")
    if not contact:
        raise OnboardingError("Ansprechpartner fehlt.")
    if not email:
        raise OnboardingError("Kontakt-E-Mail fehlt.")

    async with AsyncSessionLocal() as session:
        existing = (await session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )).scalar_one_or_none()
        if existing is not None:
            raise OnboardingError(
                f"Ein Betrieb mit dem Slug '{slug}' existiert bereits."
            )

        tenant = Tenant(
            slug=slug,
            company_name=name,
            contact_name=contact,
            contact_email=email,
            contact_phone=phone or None,
            status=TenantStatus.ONBOARDING,
            notes=notes or None,
            branche=branche or None,
        )
        session.add(tenant)
        await session.flush()  # tenant.id verfuegbar
        tenant_id = tenant.id

        # Default-Employee (Inhaber). Pflicht — der Aktivierungs-Token
        # bindet den Telegram-Chat an genau diesen Employee.
        default_emp = Employee(
            tenant_id=tenant_id,
            slug="default",
            name=contact,
            contact_email=email,
            is_default=True,
        )
        session.add(default_emp)
        await session.flush()
        default_employee_id = default_emp.id

        # Kalender mit Defaults konfigurieren.
        kalender_config = dict(DEFAULT_KALENDER_CONFIG)
        kalender_config["betrieb_name"] = name
        session.add(ToolConfig(
            tenant_id=tenant_id,
            tool_name="kalender",
            enabled=True,
            config=kalender_config,
        ))

        # Restliche Default-Features als simple enabled-Flags.
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

    # Branchen-Template laden + als TenantKnowledge schreiben.
    knowledge_loaded = 0
    template = load_branche_template(branche)
    if template is not None:
        try:
            async with AsyncSessionLocal() as session:
                for entry in template.get("knowledge", []):
                    kat = entry.get("kategorie", "")
                    txt = entry.get("text", "")
                    if kat in ALLE_KATEGORIEN and txt:
                        session.add(TenantKnowledge(
                            tenant_id=tenant_id,
                            kategorie=kat,
                            text=txt[:2000],
                        ))
                        knowledge_loaded += 1
                await session.commit()
        except Exception:
            logger.exception("Branche-Knowledge konnte nicht geladen werden")

    activated = sorted(
        FEATURES[k].label for k in set(DEFAULT_FEATURES) | {
            k for k, f in FEATURES.items() if getattr(f, "always_on", False)
        } if k in FEATURES
    )
    logger.info(
        "Tenant angelegt: slug=%s id=%s features=%d knowledge=%d",
        slug, tenant_id, len(activated), knowledge_loaded,
    )
    return ProvisionResult(
        tenant_id=tenant_id,
        default_employee_id=default_employee_id,
        activated_features=activated,
        knowledge_loaded=knowledge_loaded,
    )


# ---------------------------------------------------------------------
# Sicherer Inhaber-Aktivierungs-Link (S13)
# ---------------------------------------------------------------------

async def _load_global_bot_token() -> str | None:
    """Zentraler Telegram-Bot-Token aus der _global telegram_bot ToolConfig.

    Bewusst hier dupliziert (statt aus dem telegram_notify-Plugin zu
    importieren), damit core nicht von plugins abhaengt — gleiches
    Muster wie in scripts/onboard.py und scripts/generate_qr.py.
    """
    async with AsyncSessionLocal() as s:
        gt = (await s.execute(
            select(Tenant).where(Tenant.slug == GLOBAL_TENANT_SLUG)
        )).scalar_one_or_none()
        if not gt:
            return None
        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == gt.id,
                ToolConfig.tool_name == "telegram_bot",
            )
        )).scalar_one_or_none()
        if not tc or not tc.enabled:
            return None
        return (tc.config or {}).get("bot_token") or None


async def _get_bot_username(bot_token: str) -> str | None:
    """@username des Bots via Telegram getMe — fuer den Deep-Link."""
    url = f"https://api.telegram.org/bot{bot_token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data.get("ok"):
                return None
            return data["result"].get("username")
    except Exception:
        logger.exception("getMe fehlgeschlagen")
        return None


async def create_owner_activation(
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    ttl_days: int = OWNER_TOKEN_TTL_DAYS,
):
    """Erzeugt einen einmaligen Aktivierungs-Token (inkl. kurzem
    short_code) fuer das Onboarding. Gibt die Token-Zeile zurueck —
    `.token` fuer den Deep-Link, `.short_code` fuer die Code-Eingabe per
    Telegram-Suche."""
    return await create_activation_token(
        tenant_id, employee_id, ttl_days=ttl_days,
    )


async def global_bot_username() -> str | None:
    """@username des geteilten Bots (fuer die Such-Anleitung in der Mail).
    Best-effort — None wenn Bot/Telegram nicht erreichbar."""
    bot_token = await _load_global_bot_token()
    if not bot_token:
        return None
    return await _get_bot_username(bot_token)


async def build_owner_activation_link(
    tenant_id: uuid.UUID,
    default_employee_id: uuid.UUID,
    *,
    ttl_days: int = OWNER_TOKEN_TTL_DAYS,
) -> str:
    """Erzeugt einen Aktivierungs-Token und baut den Telegram-Deep-Link
    `?start=activate_<token>` (fuer Kontexte, die einen Ein-Klick-Link
    wollen, z.B. Mitarbeiter-Detail im Bot).

    Wirft OnboardingError, wenn der geteilte Bot nicht erreichbar ist.
    """
    token_obj = await create_owner_activation(
        tenant_id, default_employee_id, ttl_days=ttl_days,
    )
    bot_username = await global_bot_username()
    if not bot_username:
        raise OnboardingError(
            "Bot-Username konnte nicht via Telegram-API geholt werden."
        )
    return f"https://t.me/{bot_username}?start=activate_{token_obj.token}"
