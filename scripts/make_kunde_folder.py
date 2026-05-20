"""Legt (idempotent) den Drive-Kundenordner an und gibt die URL aus.
Entscheidender Test, ob Drive fuer den Tenant nutzbar ist.

Aufruf (im Container):
    uv run python scripts/make_kunde_folder.py <tenant_slug> "<kunde_name>"
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant
from core.integrations.google_drive import get_or_create_kunde_folder


async def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "demo"
    kunde = sys.argv[2] if len(sys.argv) > 2 else "Max Mustermann (BEISPIEL)"
    email = sys.argv[3] if len(sys.argv) > 3 else None
    telefon = sys.argv[4] if len(sys.argv) > 4 else None
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == slug)
        )).scalar_one_or_none()
    if tenant is None:
        print(f"Tenant {slug} nicht gefunden")
        return 2
    try:
        folder_id, folder_url = await get_or_create_kunde_folder(
            tenant.id, kunde, kunde_email=email, kunde_telefon=telefon,
        )
    except Exception as e:  # noqa: BLE001
        print(f"DRIVE NICHT NUTZBAR fuer {slug}: {type(e).__name__}: {e}")
        return 3
    print(f"OK — Kundenordner fuer '{kunde}':")
    print(f"  folder_id : {folder_id}")
    print(f"  folder_url: {folder_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
