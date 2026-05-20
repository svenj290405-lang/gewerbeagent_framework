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


async def _fire_drive_upload_alert(
    *,
    tenant_id: uuid.UUID,
    count: int,
    last_reason: str,
    kunde_name: str,
) -> None:
    """Schickt Drive-Upload-Loop-Alert an Sven UND Tenant.

    Failsafe — keine Exception darf den Upload-Caller stoeren.
    """
    # Sven-Alert (admin)
    try:
        from core.integrations.admin_alerts import notify_sven_admin_alert
        await notify_sven_admin_alert(
            kind=f"drive_upload_loop.{tenant_id}",
            message=(
                f"⚠️ <b>Drive-Uploads schlagen fehl</b>\n\n"
                f"Tenant: <code>{tenant_id}</code>\n"
                f"Failures in 1h: <b>{count}</b>\n"
                f"Letzter Fehler: <code>{last_reason[:200]}</code>\n"
                f"Letzter Kunde: <code>{kunde_name[:80]}</code>"
            ),
            details={
                "tenant_id": str(tenant_id),
                "failure_count": count,
                "last_reason": last_reason[:500],
            },
        )
    except Exception as exc:
        logger.exception(f"Sven-Alert (drive_upload_loop) failed: {exc}")

    # Tenant-Push: hat eigenes Cooldown via tenant_alert
    try:
        from core.integrations.tenant_alert import (
            _send_alert, _record_alert, _was_recently_alerted,
        )
        kind = "drive_upload_loop"
        if await _was_recently_alerted(tenant_id=tenant_id, alert_kind=kind):
            return
        msg = (
            "⚠️ <b>Drive-Uploads gehen nicht durch</b>\n\n"
            "Die letzten 5 Datei-Uploads sind fehlgeschlagen. Bitte einmal "
            "den Drive-Status pruefen und bei Bedarf neu verbinden:\n\n"
            "<code>/drive_status</code>\n"
            "<code>/drive_verbinden</code>"
        )
        sent = await _send_alert(tenant_id=tenant_id, message=msg)
        await _record_alert(
            tenant_id=tenant_id, alert_kind=kind, success=sent,
            details={"failure_count": count},
        )
    except Exception as exc:
        logger.exception(f"Tenant-Alert (drive_upload_loop) failed: {exc}")


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

    # Race-Schutz: SELECT FOR UPDATE auf der Token-Zeile damit zwei
    # parallele Drive-Uploads nicht beide gleichzeitig refresh() callen
    # (Google revoked dann manchmal den alten Token sofort -> 401).
    # Pattern identisch zu core/integrations/microsoft.py:50.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OAuthToken)
            .where(OAuthToken.id == oauth_token.id)
            .with_for_update()
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

        # Re-Check: Hat ein paralleler Request schon refreshed? Dann
        # nutzen wir den frischen Token statt nochmal zu refreshen.
        now = datetime.now(timezone.utc)
        already_fresh = (
            oauth_token.access_token_expires_at is not None
            and oauth_token.access_token_expires_at > now
            and oauth_token.access_token
            and creds.token == oauth_token.access_token
        )

        if not creds.valid and creds.refresh_token and not already_fresh:
            try:
                creds.refresh(GRequest())
            except Exception as exc:
                # invalid_grant / Refresh-Token revoked? Tenant per Telegram
                # alarmieren damit er re-authorizen kann. Pipeline wirft
                # weiterhin den Fehler nach oben — der Push ist nur Sichtbar-
                # keits-Helfer, nicht Error-Suppression.
                from core.security.oauth_alert import (
                    is_oauth_invalid_error, notify_oauth_token_invalid,
                )
                if is_oauth_invalid_error(exc):
                    try:
                        await notify_oauth_token_invalid(
                            tenant_id, "google", reason=str(exc)[:200],
                        )
                    except Exception as alert_exc:  # noqa: BLE001
                        logger.warning(
                            f"oauth-alert (google) failed for tenant={tenant_id}: "
                            f"{alert_exc}"
                        )
                raise
            oauth_token.access_token = creds.token
            if creds.expiry:
                oauth_token.access_token_expires_at = creds.expiry.replace(
                    tzinfo=timezone.utc,
                )
            await session.commit()
        elif already_fresh:
            # Der Token wurde von einem parallelen Request bereits erneuert.
            # Wir bauen creds mit dem frischen access_token aus der DB neu.
            creds = Credentials(
                token=oauth_token.access_token,
                refresh_token=oauth_token.refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=scopes,
            )

    # Drive-Service synchron bauen (googleapiclient ist sync, also blockt
    # nicht — der Build-Call macht keinen Netzverkehr).
    return build("drive", "v3", credentials=creds, cache_discovery=False)


async def _ensure_root_folder(
    service, tenant,
) -> str:
    """Findet oder erstellt den Tenant-Root-Ordner. Returns folder_id.

    Drei-stufige Strategie:
    1. **DB-Cache (`tenants.drive_root_folder_id`)** — schnellster Weg
       und Naming-Drift-sicher. Wird via files.get validiert; wenn der
       gecachte Ordner geloescht wurde, falls-through.
    2. **Suche-by-Name** mit allen bekannten Namens-Varianten (Em-Dash,
       Unterstrich, ASCII-Minus) — uebernimmt einen existierenden Ordner
       und cached die ID, damit ab jetzt Schritt 1 greift.
    3. **Erstellen** mit dem aktuellen kanonischen Namen + cachen.

    So entsteht garantiert nur EIN Root-Ordner pro Tenant, auch wenn
    company_name oder die Naming-Konvention spaeter geaendert werden.
    """
    canonical_name = _root_folder_name(tenant)
    # Naming-Varianten die historisch verwendet wurden — werden bei
    # Suche-by-Name probiert um Waisen aus alten Code-Versionen zu
    # uebernehmen statt einen neuen Duplikat-Ordner zu erstellen.
    legacy_names = [
        canonical_name,                                # "Gewerbeagent — X"
        canonical_name.replace(" — ", "_ "),           # "Gewerbeagent_ X"
        canonical_name.replace(" — ", " - "),          # "Gewerbeagent - X"
    ]

    def _sync_validate_cached(folder_id: str) -> bool:
        """Prueft ob ein gecachter Folder noch existiert + nicht im Trash."""
        try:
            meta = service.files().get(
                fileId=folder_id, fields="id, trashed",
            ).execute()
            return not meta.get("trashed", False)
        except Exception:
            return False

    def _sync_find_by_names() -> str | None:
        for name in legacy_names:
            escaped = name.replace("'", "\\'")
            q = (
                f"name='{escaped}' "
                f"and mimeType='{DRIVE_FOLDER_MIME}' "
                f"and trashed=false"
            )
            try:
                res = service.files().list(
                    q=q, spaces="drive",
                    fields="files(id, name)",
                    pageSize=10,
                ).execute()
                files = res.get("files", [])
                if files:
                    return files[0]["id"]
            except Exception as e:
                logger.warning(f"Root-Folder-Suche '{name}' failed: {e}")
        return None

    def _sync_create() -> str:
        meta = {"name": canonical_name, "mimeType": DRIVE_FOLDER_MIME}
        created = service.files().create(body=meta, fields="id").execute()
        return created["id"]

    # 1. DB-Cache
    cached_id = getattr(tenant, "drive_root_folder_id", None)
    if cached_id:
        if await asyncio.to_thread(_sync_validate_cached, cached_id):
            return cached_id
        logger.info(
            f"Cached Drive-Root-Folder {cached_id} fuer tenant={tenant.slug} "
            "ist weg (trashed/deleted) — suche/erstelle neu."
        )

    # 2. Suche-by-Name (alle Naming-Varianten)
    found_id = await asyncio.to_thread(_sync_find_by_names)
    if not found_id:
        # 3. Erstellen
        found_id = await asyncio.to_thread(_sync_create)
        logger.info(
            f"Drive-Root-Folder '{canonical_name}' neu erstellt "
            f"(id={found_id}) fuer tenant={tenant.slug}"
        )

    # Cache in DB schreiben damit Schritt 1 ab jetzt greift
    async with AsyncSessionLocal() as s:
        db_tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tenant.id)
        )).scalar_one_or_none()
        if db_tenant is not None and db_tenant.drive_root_folder_id != found_id:
            db_tenant.drive_root_folder_id = found_id
            await s.commit()

    return found_id


def _kunde_identity_key(
    kunde_name: str,
    kunde_email: str | None = None,
    kunde_telefon: str | None = None,
) -> str:
    """Stabile Kunden-Identitaet fuer den Ordner-Key:
    E-Mail > Telefon (normalisiert) > Namens-Slug (Fallback).

    So teilen sich zwei gleichnamige Kunden NICHT denselben Ordner, und
    dieselbe Person (gleiche Mail/Telefon) trifft ihren Ordner auch bei
    leicht abweichendem Namen wieder.
    """
    email = (kunde_email or "").strip().lower()
    if email:
        return f"email:{email}"[:120]
    if kunde_telefon:
        from core.utils.phone import normalize_phone
        tel = normalize_phone(kunde_telefon)
        if tel:
            return f"tel:{tel}"[:120]
    return _slugify_kunde(kunde_name)[:120]


async def get_or_create_kunde_folder(
    tenant_id: uuid.UUID,
    kunde_name: str,
    employee_id: uuid.UUID | None = None,
    *,
    kunde_email: str | None = None,
    kunde_telefon: str | None = None,
) -> tuple[str, str]:
    """Liefert (folder_id, folder_url) fuer den Kunden-Drive-Ordner.

    Identitaet ueber E-Mail/Telefon (siehe _kunde_identity_key) statt nur
    Name — zwei gleichnamige Kunden bekommen getrennte Ordner, dieselbe
    Person denselben. Ordner-Anzeigename = voller Kundenname.

    1. DB-Lookup nach Identitaets-Key
    2. Adoption: kein Identitaets-Treffer, aber Legacy-Ordner unter dem
       Namens-Slug -> uebernehmen + auf Identitaets-Key umschluesseln
    3. Treffer: cache + Validierung (Drive-Trash-Recovery)
    4. Sonst: Drive-Folder unter Tenant-Root erstellen, persistieren

    Race-Schutz: SELECT FOR UPDATE auf der DB-Zeile.
    """
    email_norm = (kunde_email or "").strip().lower() or None
    tel_norm = None
    if kunde_telefon:
        from core.utils.phone import normalize_phone
        tel_norm = normalize_phone(kunde_telefon) or None
    kunde_key = _kunde_identity_key(kunde_name, email_norm, tel_norm)
    name_slug = _slugify_kunde(kunde_name)

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

        # Adoption: Legacy-Ordner (unter Namens-Slug) auf den Identitaets-
        # Key umschluesseln — einmalige Migration beim ersten Mail/Tel-
        # Zugriff, damit Bestandsordner nicht verwaisen.
        if existing is None and kunde_key != name_slug:
            legacy = (await s.execute(
                select(TenantKundeDrive)
                .where(TenantKundeDrive.tenant_id == tenant_id)
                .where(TenantKundeDrive.kunde_key == name_slug)
                .with_for_update()
            )).scalar_one_or_none()
            if legacy is not None:
                legacy.kunde_key = kunde_key
                legacy.kunde_email = email_norm
                legacy.kunde_telefon = tel_norm
                if kunde_name:
                    legacy.kunde_name = kunde_name
                await s.commit()
                logger.info(
                    f"Kundenordner adoptiert: {name_slug!r} -> {kunde_key!r} "
                    f"(tenant={tenant_id})"
                )
                existing = legacy

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

    sub_name = (kunde_name or "Kunde")[:200]  # voller Name als Anzeigename

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
                kunde_email=email_norm,
                kunde_telefon=tel_norm,
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
    *,
    kunde_email: str | None = None,
    kunde_telefon: str | None = None,
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

    Failure-Counter (Phase-A A4): wenn 5 Uploads pro Tenant in einer
    Stunde fehlschlagen, geht ein Sven-Alert UND eine Tenant-Push-
    Notification raus. Bei Erfolg wird der Counter zurueckgesetzt.
    """
    from core.integrations.failure_counter import DRIVE_UPLOAD_FAILURES

    try:
        folder_id, folder_url = await get_or_create_kunde_folder(
            tenant_id, kunde_name, employee_id=employee_id,
            kunde_email=kunde_email, kunde_telefon=kunde_telefon,
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
        # Erfolg — Failure-Window fuer diesen Tenant zuruecksetzen.
        DRIVE_UPLOAD_FAILURES.reset(key=str(tenant_id))
    except Exception as exc:
        # Failure-Counter aktualisieren + ggf. Alert ausloesen.
        should_alert, count = DRIVE_UPLOAD_FAILURES.record_failure(
            key=str(tenant_id), reason=f"{type(exc).__name__}: {exc}",
        )
        if should_alert:
            await _fire_drive_upload_alert(
                tenant_id=tenant_id, count=count, last_reason=str(exc)[:200],
                kunde_name=kunde_name,
            )
        # Original-Exception weitergeben — Caller meldet im Telegram.
        raise

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
