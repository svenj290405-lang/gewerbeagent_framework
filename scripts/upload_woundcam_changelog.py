"""Einmaliges Skript: laedt die WoundCam-Aenderungsliste als NEUE Datei in
Svens Drive hoch. Nutzt die Tenant-Drive-Anbindung (Tenant `pilot`, Konto
svenj290405@gmail.com) — vorbild: scripts/upload_legal_to_drive.py.

Lauf (im Container):
    docker exec -w /app gewerbeagent_framework uv run python \
        scripts/upload_woundcam_changelog.py
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from core.plugin_system.registry import discover_plugins

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app/_woundcam_changelog_tmp.txt")
TENANT_SLUG = "pilot"
DRIVE_NAME = sys.argv[2] if len(sys.argv) > 2 else "WoundCam_Aenderungen_seit_Lohscheller.txt"
FILE_ID = sys.argv[3] if len(sys.argv) > 3 else None  # gesetzt = in-place update
MIME = "text/plain"


async def main() -> int:
    discover_plugins()

    from sqlalchemy import select
    from googleapiclient.http import MediaIoBaseUpload

    from core.database import AsyncSessionLocal
    from core.models import Tenant
    from core.integrations.google_drive import get_drive_service

    if not SRC.exists():
        print(f"Quelle fehlt: {SRC}")
        return 2

    async with AsyncSessionLocal() as s:
        tenant = (
            await s.execute(select(Tenant).where(Tenant.slug == TENANT_SLUG))
        ).scalar_one_or_none()
    if tenant is None:
        print(f"Tenant {TENANT_SLUG} nicht gefunden")
        return 2

    service = await get_drive_service(tenant.id)
    data = SRC.read_bytes()

    def _upload():
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=MIME, resumable=False)
        if FILE_ID:
            return service.files().update(
                fileId=FILE_ID, media_body=media,
                fields="id, name, webViewLink, modifiedTime",
            ).execute()
        return service.files().create(
            body={"name": DRIVE_NAME},
            media_body=media,
            fields="id, name, webViewLink",
        ).execute()

    up = await asyncio.to_thread(_upload)
    print(f"hochgeladen: {up['name']}  id={up['id']}")
    print(f"link: {up.get('webViewLink')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
