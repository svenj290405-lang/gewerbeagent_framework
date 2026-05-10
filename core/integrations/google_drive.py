"""
Google-Drive-Integration fuer Kunden-Daten-Archiv.

Pro Kunde wird ein Sub-Ordner unter einem Tenant-Root-Ordner erstellt.
Telegram-Uploads landen dort. /briefing zeigt den Drive-Link.

Folder-Struktur:
  📁 Gewerbeagent — <Company-Name>     (Root, einmalig pro Tenant)
    ├─ 📁 Mueller (mueller)            (lazy-erstellt beim ersten Upload)
    ├─ 📁 Schmidt GmbH (schmidt-gmbh)
    └─ ...

Nutzt drive.file-Scope: Q sieht nur Ordner die er selbst erstellt hat.
Tenant kann diese in Drive aber unter jeden Ordner verschieben — das
bricht die Funktionalitaet nicht weil wir nur via folder_id arbeiten.

Failsafe-Pattern: alle Drive-Calls sind try-except gewrapped. Drive-
Fehler brechen NIE den Caller (z.B. /briefing zeigt halt keinen Link).
"""
from __future__ import annotations

import asyncio
import io
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from core.database import AsyncSessionLocal
from core.models.tenant_kunde_drive import TenantKundeDrive
from core.models import Tenant

logger = logging.getLogger(__name__)


DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


def _slugify_kunde(name: str) -> str:
    """'Müller-Bauunternehmen GmbH' -> 'mueller-bauunternehmen-gmbh'."""
    s = (name or "").strip().lower()
    s = (s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
         .replace("ß", "ss"))
    out = []
    last_dash = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    result = "".join(out).strip("-")
    return result[:120] or "kunde"


def _root_folder_name(tenant) -> str:
    company = (tenant.company_name or tenant.slug or "Tenant").strip()
    return f"Gewerbeagent — {company}"[:200]


async def get_drive_service(
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID | None = None,
):
    """Liefert authentifizierten Google-Drive-Service.

    Wirft ValueError wenn der Tenant noch keinen OAuth-Token mit
    drive-Scope hat (Tenant muss /drive_verbinden machen).
    """
    from core.security.oauth_token_lookup import find_oauth_token
    from plugins.kalender.google_auth import _get_google_client_creds
    from google.auth.transport.requests import Request as GRequest
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from core.models import OAuthToken

    oauth_token = await find_oauth_token(tenant_id, "google", employee_id)
    if not oauth_token:
        raise ValueError(
            "Kein Google-OAuth-Token vorhanden. "
            "Bitte /drive_verbinden im Telegram ausfuehren."
        )

    # Pruefen ob drive-Scope drin ist (sonst hat Tenant noch nicht
    # re-auth gemacht)
    scopes = (oauth_token.scopes or "").split(",")
    if not any("drive" in s for s in scopes):
        raise ValueError(
            "Google-Token hat keinen Drive-Scope. "
            "Bitte einmal /drive_verbinden im Telegram ausfuehren."
        )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OAuthToken).where(OAuthToken.id == oauth_token.id)
        )
        oauth_token = result.scalar_one()

        client_id, client_secret = _get_google_client_creds()
        creds = Credentials(
            token=oauth_token.access_token,
            refresh_token=oauth_token.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
        )
        if not creds.valid and creds.refresh_token:
            creds.refresh(GRequest())
            oauth_token.access_token = creds.token
            if creds.expiry:
                oauth_token.access_token_expires_at = creds.expiry.replace(
                    tzinfo=timezone.utc,
                )
            await session.commit()

    # Drive-Service synchron bauen (googleapiclient ist sync, also blockt
    # nicht — der Build-Call macht keinen Netzverkehr).
    return build("drive", "v3", credentials=creds, cache_discovery=False)


async def _ensure_root_folder(
    service, tenant,
) -> str:
    """Findet oder erstellt den Tenant-Root-Ordner. Returns folder_id.

    Cache-Strategie: wir suchen nach Folder mit dem Namen — wenn nicht
    gefunden, erstellen. Damit ueberlebt das auch wenn der Tenant den
    DB-Mapping verliert (selten).
    """
    root_name = _root_folder_name(tenant)

    def _sync_find_or_create():
        # Suche nach existierendem Root.
        # In Drive-Query muss ' escaped werden. Wir nutzen str.replace
        # vor dem f-String weil f-Strings keine Backslashes erlauben.
        escaped_name = root_name.replace("'", "\\'")
        q = (
            f"name='{escaped_name}' "
            f"and mimeType='{DRIVE_FOLDER_MIME}' "
            f"and trashed=false"
        )
        try:
            res = service.files().list(
                q=q, spaces="drive",
                fields="files(id, name, parents)",
                pageSize=10,
            ).execute()
            files = res.get("files", [])
            if files:
                return files[0]["id"]
        except Exception as e:
            logger.warning(f"Root-Folder-Suche failed (egal, create): {e}")

        # Nicht da: erstellen
        meta = {"name": root_name, "mimeType": DRIVE_FOLDER_MIME}
        created = service.files().create(
            body=meta, fields="id",
        ).execute()
        return created["id"]

    return await asyncio.to_thread(_sync_find_or_create)


async def get_or_create_kunde_folder(
    tenant_id: uuid.UUID,
    kunde_name: str,
    employee_id: uuid.UUID | None = None,
) -> tuple[str, str]:
    """Liefert (folder_id, folder_url) fuer den Kunden-Drive-Ordner.

    1. DB-Lookup tenant_kunde_drive
    2. Wenn vorhanden: returnt cache + Validierung dass Folder noch
       existiert (Drive-Trash-Recovery)
    3. Wenn nicht: erstellt Drive-Folder unter Tenant-Root, persistiert.

    Race-Schutz: SELECT FOR UPDATE auf der DB-Zeile damit zwei parallele
    Uploads nicht zwei Folder erstellen.
    """
    kunde_key = _slugify_kunde(kunde_name)

    # Tenant fuer Root-Folder-Name laden
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )).scalar_one_or_none()
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} nicht gefunden")

    # Atomarer Lookup-or-Create (locked row)
    async with AsyncSessionLocal() as s:
        existing = (await s.execute(
            select(TenantKundeDrive)
            .where(TenantKundeDrive.tenant_id == tenant_id)
            .where(TenantKundeDrive.kunde_key == kunde_key)
            .with_for_update()
        )).scalar_one_or_none()

        if existing:
            # Sanity-Check: Folder existiert noch in Drive?
            # Wenn 404: alten Eintrag loeschen, Re-Create am Ende.
            try:
                service = await get_drive_service(tenant_id, employee_id)
                await asyncio.to_thread(
                    lambda: service.files().get(
                        fileId=existing.drive_folder_id,
                        fields="id, trashed",
                    ).execute()
                )
                # Existiert. Folder verwenden.
                return existing.drive_folder_id, existing.drive_folder_url
            except Exception as e:
                err_str = str(e)
                if "404" in err_str or "notFound" in err_str:
                    logger.info(
                        f"Drive-Folder {existing.drive_folder_id} fuer Kunde "
                        f"{kunde_name!r} fehlt — erstelle neu"
                    )
                    await s.delete(existing)
                    await s.commit()
                    # Fall through zum Re-Create
                else:
                    # Anderer Fehler — geben wir trotzdem die alte URL
                    # zurueck, damit /briefing nicht bricht.
                    logger.warning(
                        f"Drive-Folder-Check fehlgeschlagen (return cached): {e}"
                    )
                    return existing.drive_folder_id, existing.drive_folder_url

    # Nicht vorhanden ODER war 404 -> Drive-Folder erstellen
    service = await get_drive_service(tenant_id, employee_id)
    root_id = await _ensure_root_folder(service, tenant)

    sub_name = f"{kunde_name} ({kunde_key})"[:200]

    def _create_subfolder():
        meta = {
            "name": sub_name,
            "mimeType": DRIVE_FOLDER_MIME,
            "parents": [root_id],
        }
        return service.files().create(body=meta, fields="id, webViewLink").execute()

    new_folder = await asyncio.to_thread(_create_subfolder)
    folder_id = new_folder["id"]
    folder_url = (
        new_folder.get("webViewLink")
        or f"https://drive.google.com/drive/folders/{folder_id}"
    )

    # Persistieren
    async with AsyncSessionLocal() as s:
        try:
            row = TenantKundeDrive(
                tenant_id=tenant_id,
                kunde_key=kunde_key,
                kunde_name=kunde_name,
                drive_folder_id=folder_id,
                drive_folder_url=folder_url,
                upload_count=0,
            )
            s.add(row)
            await s.commit()
        except Exception as e:
            # Race: anderer Request hat schon einen Eintrag erstellt.
            # Wir nutzen seinen Folder, koennten unseren erstellten in
            # Drive verwaisen — Tenant kann manuell aufraeumen.
            logger.warning(f"Race beim Folder-Persist (egal): {e}")
            await s.rollback()
            existing = (await s.execute(
                select(TenantKundeDrive)
                .where(TenantKundeDrive.tenant_id == tenant_id)
                .where(TenantKundeDrive.kunde_key == kunde_key)
            )).scalar_one_or_none()
            if existing:
                return existing.drive_folder_id, existing.drive_folder_url
            raise

    return folder_id, folder_url


async def upload_file_to_kunde_folder(
    tenant_id: uuid.UUID,
    kunde_name: str,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    employee_id: uuid.UUID | None = None,
) -> dict:
    """Uploadet eine Datei in den Kunden-Ordner.

    Returns:
        {
            'file_id': str,
            'web_link': str,
            'kunde_folder_id': str,
            'kunde_folder_url': str,
            'upload_count': int,  # neu nach Inkrement
        }
    Raises bei Fehlern (Caller fängt + meldet im Telegram).
    """
    folder_id, folder_url = await get_or_create_kunde_folder(
        tenant_id, kunde_name, employee_id=employee_id,
    )

    service = await get_drive_service(tenant_id, employee_id)

    from googleapiclient.http import MediaIoBaseUpload

    def _sync_upload():
        media = MediaIoBaseUpload(
            io.BytesIO(file_bytes), mimetype=mime_type, resumable=False,
        )
        meta = {"name": filename[:200], "parents": [folder_id]}
        return service.files().create(
            body=meta, media_body=media,
            fields="id, webViewLink, name",
        ).execute()

    uploaded = await asyncio.to_thread(_sync_upload)

    # upload_count + last_upload_at hochzaehlen
    new_count = 0
    try:
        async with AsyncSessionLocal() as s:
            row = (await s.execute(
                select(TenantKundeDrive)
                .where(TenantKundeDrive.tenant_id == tenant_id)
                .where(TenantKundeDrive.drive_folder_id == folder_id)
                .with_for_update()
            )).scalar_one_or_none()
            if row:
                row.upload_count = (row.upload_count or 0) + 1
                row.last_upload_at = datetime.now(timezone.utc)
                new_count = row.upload_count
                await s.commit()
    except Exception as e:
        logger.debug(f"upload_count-update failed (egal): {e}")

    return {
        "file_id": uploaded["id"],
        "web_link": uploaded.get("webViewLink") or "",
        "kunde_folder_id": folder_id,
        "kunde_folder_url": folder_url,
        "upload_count": new_count,
    }


async def get_kunde_folder_link(
    tenant_id: uuid.UUID,
    kunde_name: str,
) -> str | None:
    """Read-only Lookup fuer /briefing + /kunde.

    Erstellt KEIN neues Folder. Nur Cache-Hit. Failsafe — None bei
    Fehlern.
    """
    try:
        kunde_key = _slugify_kunde(kunde_name)
        async with AsyncSessionLocal() as s:
            row = (await s.execute(
                select(TenantKundeDrive)
                .where(TenantKundeDrive.tenant_id == tenant_id)
                .where(TenantKundeDrive.kunde_key == kunde_key)
            )).scalar_one_or_none()
            return row.drive_folder_url if row else None
    except Exception as e:
        logger.debug(f"get_kunde_folder_link failed (egal): {e}")
        return None


async def list_tenant_kunde_drives(
    tenant_id: uuid.UUID, limit: int = 30,
) -> list[TenantKundeDrive]:
    """Liefert die letzten Kunden-Ordner sortiert nach last_upload_at.

    Fuer /archiv ohne Argument (Liste aller Kunden mit Drive-Ordner)."""
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(TenantKundeDrive)
            .where(TenantKundeDrive.tenant_id == tenant_id)
            .order_by(
                TenantKundeDrive.last_upload_at.desc().nullslast(),
                TenantKundeDrive.kunde_name.asc(),
            )
            .limit(limit)
        )).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def is_drive_configured(oauth_token) -> bool:
    """True wenn der Token einen Drive-Scope enthaelt."""
    if not oauth_token:
        return False
    scopes = (getattr(oauth_token, "scopes", "") or "").split(",")
    return any("drive" in s for s in scopes)


__all__ = [
    "get_drive_service",
    "get_or_create_kunde_folder",
    "upload_file_to_kunde_folder",
    "get_kunde_folder_link",
    "list_tenant_kunde_drives",
    "is_drive_configured",
    "_slugify_kunde",
]
