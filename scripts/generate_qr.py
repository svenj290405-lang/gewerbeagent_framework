"""
Generiert einen QR-Code fuer Tenant-Onboarding via Telegram-Bot.

Usage:
    docker compose exec framework uv run python -m scripts.generate_qr <tenant_slug>

Output:
    /tmp/qr_<slug>.png  -- der QR-Code zum Scannen oder Versenden
    + Konsolen-Info (Tenant-Daten + Deep-Link)
"""
import asyncio
import os
import sys
from pathlib import Path

import qrcode
from sqlalchemy import select

sys.path.insert(0, "/app")
from core.database import AsyncSessionLocal
from core.models import Tenant, ToolConfig


async def get_bot_username(bot_token: str) -> str | None:
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


async def main():
    if len(sys.argv) < 2:
        print("ERROR: Tenant-Slug fehlt")
        print("Usage: python -m scripts.generate_qr <tenant_slug>")
        sys.exit(1)

    tenant_slug = sys.argv[1].strip().lower()

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )).scalar_one_or_none()
        if not tenant:
            print(f"ERROR: Tenant '{tenant_slug}' nicht gefunden")
            sys.exit(1)
        if tenant_slug == "_global":
            print("ERROR: _global ist kein Endkunden-Tenant")
            sys.exit(1)

        global_tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == "_global")
        )).scalar_one()
        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == global_tenant.id,
                ToolConfig.tool_name == "telegram_bot",
            )
        )).scalar_one_or_none()
        if not tc or not tc.enabled:
            print("ERROR: telegram_bot ToolConfig in _global fehlt oder deaktiviert")
            sys.exit(1)
        bot_token = (tc.config or {}).get("bot_token")
        if not bot_token:
            print("ERROR: bot_token in _global telegram_bot ToolConfig leer")
            sys.exit(1)

    bot_username = await get_bot_username(bot_token)
    if not bot_username:
        print("ERROR: Bot-Username konnte nicht via Telegram-API geholt werden")
        sys.exit(1)

    deep_link = f"https://t.me/{bot_username}?start={tenant_slug}"

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

    print()
    print("=" * 60)
    print(f"  QR-Code generiert fuer Tenant: {tenant_slug}")
    print("=" * 60)
    print(f"  Firma:       {tenant.company_name}")
    print(f"  Status:      {tenant.status}")
    print(f"  Bot:         @{bot_username}")
    print(f"  Deep-Link:   {deep_link}")
    print(f"  Chat-ID:     {tenant.telegram_chat_id or '(noch nicht verbunden)'}")
    print(f"  PNG-Datei:   {out_path}")
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
