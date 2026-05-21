"""READ-ONLY Diagnose: Hat pilot einen eigenen telegram_notify.bot_token?
Zeigt nur Präsenz/Länge/Fernet-Indiz, nie den Token-Wert."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant
from core.models.tool_config import ToolConfig


def _describe(val):
    if not val:
        return "FEHLT/leer"
    s = str(val)
    looks_fernet = s.startswith("gAAAA")
    looks_plain_bot = (":" in s and s.split(":", 1)[0].isdigit())
    kind = "Fernet-verschlüsselt" if looks_fernet else (
        "Klartext-Bot-Token" if looks_plain_bot else "unbekanntes Format")
    return f"vorhanden (len={len(s)}, {kind})"


async def main():
    async with AsyncSessionLocal() as s:
        for slug in ("pilot", "_global"):
            t = (await s.execute(
                select(Tenant).where(Tenant.slug == slug)
            )).scalar_one_or_none()
            if t is None:
                print(f"{slug}: kein Tenant")
                continue
            tcs = (await s.execute(
                select(ToolConfig).where(
                    ToolConfig.tenant_id == t.id,
                    ToolConfig.tool_name.in_(["telegram_notify", "telegram_bot"]),
                )
            )).scalars().all()
            print(f"\n[{slug}]")
            if not tcs:
                print("  (keine telegram_notify/telegram_bot ToolConfig)")
            for tc in tcs:
                cfg = tc.config or {}
                print(f"  tool={tc.tool_name} enabled={tc.enabled} "
                      f"keys={sorted(cfg.keys())}")
                print(f"     bot_token: {_describe(cfg.get('bot_token'))}")
                if "chat_id" in cfg:
                    print(f"     chat_id(legacy): {cfg.get('chat_id')!r}")


if __name__ == "__main__":
    asyncio.run(main())
