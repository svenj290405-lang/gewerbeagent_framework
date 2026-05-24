"""
Generiert einen QR-Code fuer Tenant-Onboarding via Telegram-Bot.

CLI:
    docker compose exec framework uv run python -m scripts.generate_qr <tenant_slug>

API (B1-3):
    from scripts.generate_qr import generate_for_slug
    pngpath = await generate_for_slug("dietz")

Output:
    /tmp/qr_<slug>.png  -- der QR-Code zum Scannen oder Versenden
    + Konsolen-Info (Tenant-Daten + Deep-Link), wenn via CLI aufgerufen
"""
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import qrcode
from sqlalchemy import select

sys.path.insert(0, "/app")
from core.database import AsyncSessionLocal
from core.models import Tenant, ToolConfig, Employee, create_activation_token


@dataclass
class QRResult:
    """Was generate_for_slug zurueckliefert."""
    png_path: Path
    deep_link: str
    bot_username: str
    tenant_slug: str
    tenant_company: str


async def _get_bot_username(bot_token: str) -> str | None:
    """Holt den Bot-Username via Telegram-API (fuer den Deep-Link)."""
    import httpx
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
        return None


async def generate_for_slug(slug: str) -> QRResult:
    """Hauptfunktion — wiederverwendbar aus onboard.py + CLI.

    Wirft ValueError bei Fehlern (Tenant fehlt, Bot-Token fehlt,
    Telegram-API gibt nix zurueck). CLI-Wrapper fangt die Errors
    und mapped sie auf sys.exit-Codes.
    """
    tenant_slug = (slug or "").strip().lower()
    if not tenant_slug:
        raise ValueError("Tenant-Slug ist leer")
    if tenant_slug == "_global":
        raise ValueError("_global ist kein Endkunden-Tenant")

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )).scalar_one_or_none()
        if not tenant:
            raise ValueError(f"Tenant '{tenant_slug}' nicht gefunden")

        # S13: Default-Employee (Inhaber) fuer den Token-Link bestimmen.
        default_emp = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant.id,
                Employee.is_default == True,  # noqa: E712
            )
        )).scalar_one_or_none()
        if default_emp is None:
            raise ValueError(
                f"Tenant '{tenant_slug}' hat keinen Default-Employee — "
                "Onboarding-Link nicht erzeugbar."
            )
        tenant_id_for_token = tenant.id
        default_emp_id = default_emp.id

        global_tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == "_global")
        )).scalar_one_or_none()
        if not global_tenant:
            raise ValueError("_global-Tenant fehlt (Infra-Setup unvollstaendig)")

        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == global_tenant.id,
                ToolConfig.tool_name == "telegram_bot",
            )
        )).scalar_one_or_none()
        if not tc or not tc.enabled:
            raise ValueError(
                "telegram_bot ToolConfig in _global fehlt oder deaktiviert"
            )
        bot_token = (tc.config or {}).get("bot_token")
        if not bot_token:
            raise ValueError(
                "bot_token in _global telegram_bot ToolConfig leer"
            )

    bot_username = await _get_bot_username(bot_token)
    if not bot_username:
        raise ValueError(
            "Bot-Username konnte nicht via Telegram-API geholt werden"
        )

    # S13: sicherer Einmal-Token-Link statt ratbarem ?start=<slug>.
    token_obj = await create_activation_token(
        tenant_id_for_token, default_emp_id, ttl_days=14,
    )
    deep_link = f"https://t.me/{bot_username}?start=activate_{token_obj.token}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(deep_link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    out_path = Path(f"/tmp/qr_{tenant_slug}.png")
    img.save(out_path)

    return QRResult(
        png_path=out_path,
        deep_link=deep_link,
        bot_username=bot_username,
        tenant_slug=tenant_slug,
        tenant_company=tenant.company_name,
    )


async def main():
    if len(sys.argv) < 2:
        print("ERROR: Tenant-Slug fehlt")
        print("Usage: python -m scripts.generate_qr <tenant_slug>")
        sys.exit(1)

    try:
        result = await generate_for_slug(sys.argv[1])
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Tenant fuer chat-id-Anzeige nochmal laden (war im Helper nicht
    # nach aussen propagiert)
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == result.tenant_slug)
        )).scalar_one()

    print()
    print("=" * 60)
    print(f"  QR-Code generiert fuer Tenant: {result.tenant_slug}")
    print("=" * 60)
    print(f"  Firma:       {result.tenant_company}")
    print(f"  Status:      {tenant.status}")
    print(f"  Bot:         @{result.bot_username}")
    print(f"  Deep-Link:   {result.deep_link}")
    print(f"  Chat-ID:     {tenant.telegram_chat_id or '(noch nicht verbunden)'}")
    print(f"  PNG-Datei:   {result.png_path}")
    print("=" * 60)
    print()
    print("So gehts weiter:")
    print(f"  1. PNG an den Handwerker schicken (Mail/Druck/SMS)")
    print(f"  2. Handwerker scannt mit Handy")
    print(f"  3. Telegram oeffnet sich, [Start] tippen")
    print(f"  4. Chat-ID wird automatisch dem Tenant zugeordnet")
    print()


if __name__ == "__main__":
    asyncio.run(main())
