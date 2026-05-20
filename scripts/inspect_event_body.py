"""Liest den echten Body/Notizen eines Outlook-Events via Graph aus.

Aufruf (im Container):
    uv run python scripts/inspect_event_body.py <tenant_slug> <event_id>
"""
from __future__ import annotations

import asyncio
import sys

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant
from core.integrations.microsoft_calendar import (
    GRAPH_API_BASE, HTTP_TIMEOUT_SECONDS, get_microsoft_token,
)


async def main() -> int:
    slug = sys.argv[1]
    event_id = sys.argv[2]
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == slug)
        )).scalar_one_or_none()
    if tenant is None:
        print(f"Tenant {slug} nicht gefunden")
        return 2
    token = await get_microsoft_token(tenant.id, employee_id=None)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as c:
        r = await c.get(
            f"{GRAPH_API_BASE}/me/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"$select": "subject,body,bodyPreview"},
        )
        print("HTTP status:", r.status_code)
        try:
            d = r.json()
        except Exception:
            print("raw:", r.text[:500])
            return 3
        print("subject     :", d.get("subject"))
        print("bodyPreview :", repr(d.get("bodyPreview")))
        print("body.content:")
        print((d.get("body") or {}).get("content"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
