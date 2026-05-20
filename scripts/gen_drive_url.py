"""Erzeugt direkt eine Google-OAuth-Connect-URL fuer einen Tenant
(umgeht den Telegram-Button). State + PKCE werden in der DB persistiert,
der Callback laeuft normal ueber /oauth/callback.

Aufruf (im Container):
    uv run python scripts/gen_drive_url.py [tenant_slug]
"""
from __future__ import annotations

import asyncio
import sys

from core.security.oauth_flow import generate_auth_url


async def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "demo"
    url = await generate_auth_url(slug, "google")
    print(f"CONNECT-URL fuer Tenant '{slug}':")
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
