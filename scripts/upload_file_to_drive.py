"""Laedt eine beliebige Datei in das Google Drive eines Tenants hoch
(nutzt die bestehende drive.file-OAuth-Anbindung von get_drive_service).

Aufruf (im Container):
    # 1) Trockenlauf — zeigt NUR, in welches Google-Konto hochgeladen wuerde:
    uv run python scripts/upload_file_to_drive.py <datei_pfad> [tenant_slug]
    # 2) Echter Upload (erst nach Konto-Check!):
    uv run python scripts/upload_file_to_drive.py <datei_pfad> [tenant_slug] --confirm

Default-Slug: pilot. Ohne --confirm wird NICHTS hochgeladen.
"""
from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant
from core.integrations.google_drive import get_drive_service


async def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--confirm"]
    confirm = "--confirm" in sys.argv
    if not args:
        print("Usage: upload_file_to_drive.py <datei_pfad> [tenant_slug] [--confirm]")
        return 2
    file_path = args[0]
    slug = args[1] if len(args) > 1 else "pilot"

    if not os.path.isfile(file_path):
        print(f"Datei nicht gefunden: {file_path}")
        return 2

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == slug)
        )).scalar_one_or_none()
    if tenant is None:
        print(f"Tenant {slug!r} nicht gefunden")
        return 2

    try:
        service = await get_drive_service(tenant.id)
    except Exception as e:  # noqa: BLE001
        print(f"DRIVE NICHT NUTZBAR fuer {slug}: {type(e).__name__}: {e}")
        return 3

    # Sicherheits-Check: in welches Konto wuerde hochgeladen?
    try:
        about = service.about().get(
            fields="user(emailAddress,displayName)"
        ).execute()
        user = about.get("user", {})
        print(f"Tenant      : {slug}")
        print(f"Ziel-Konto  : {user.get('displayName')} <{user.get('emailAddress')}>")
    except Exception as e:  # noqa: BLE001
        print(f"(Konnte Konto nicht ermitteln: {e})")

    if not confirm:
        size_mb = os.path.getsize(file_path) / 1_048_576
        print(f"Datei       : {os.path.basename(file_path)} ({size_mb:.1f} MB)")
        print("TROCKENLAUF — nichts hochgeladen. Mit --confirm wiederholen.")
        return 0

    from googleapiclient.http import MediaFileUpload
    fname = os.path.basename(file_path)
    media = MediaFileUpload(file_path, resumable=True)
    file = service.files().create(
        body={"name": fname}, media_body=media,
        fields="id,name,webViewLink,size",
    ).execute()
    print("OK — hochgeladen:")
    print(f"  name       : {file.get('name')}")
    print(f"  id         : {file.get('id')}")
    print(f"  groesse    : {file.get('size')}")
    print(f"  webViewLink: {file.get('webViewLink')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
