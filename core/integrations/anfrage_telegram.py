"""Telegram-Push + Drive-Archiv wenn Kunde Anfrage-Formular abschickt."""
from __future__ import annotations

import logging
from datetime import datetime
from html import escape as _h
from zoneinfo import ZoneInfo

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import AnfrageToken, Tenant

logger = logging.getLogger(__name__)

_BERLIN_TZ = ZoneInfo("Europe/Berlin")


def _format_value(value) -> str:
    """Formatiert einen Antwort-Wert + escaped HTML.

    Wichtig: alle Kunden-Antworten landen in einer Telegram-HTML-
    Nachricht (parse_mode="HTML"). Ohne Escape koennte ein Angreifer
    mit boesartigem Form-Input fremde Bot-Antworten injizieren oder
    falsch verschachteltes HTML generieren.
    """
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and v.get("filename") for v in value):
            return f"📎 {len(value)} Datei(en)"
        return _h(", ".join(str(v) for v in value if v))
    return _h(str(value or ""))


def _format_value_plain(value) -> str:
    """Plain-Text-Variante von _format_value (kein HTML-Escape).

    Wird fuer den Drive-Text-File-Export gebraucht — dort soll der
    Handwerker den Originaltext lesen, nicht '&lt;'-Sequenzen.
    """
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and v.get("filename") for v in value):
            names = [v.get("filename", "?") for v in value]
            return f"{len(value)} Datei(en): " + ", ".join(names)
        return ", ".join(str(v) for v in value if v)
    return str(value or "")


def _extract_files(antworten: dict) -> list[dict]:
    """Sammelt alle hochgeladenen Files aus den Antworten."""
    files = []
    for key, value in antworten.items():
        if not isinstance(value, list):
            continue
        for v in value:
            if isinstance(v, dict) and v.get("filename") and v.get("base64"):
                files.append({
                    "field": key,
                    "filename": v["filename"],
                    "content_type": v.get("content_type") or "application/octet-stream",
                    "base64": v["base64"],
                })
    return files


async def _send_file_to_telegram(
    *, bot_token: str, chat_id, file_obj: dict,
) -> bool:
    """Sendet eine einzelne Datei an Telegram via sendPhoto oder sendDocument."""
    import base64 as _b64
    import httpx
    try:
        raw = _b64.b64decode(file_obj["base64"])
    except Exception:
        return False
    ct = (file_obj.get("content_type") or "").lower()
    name = file_obj.get("filename") or "datei"
    is_image = ct.startswith("image/")
    endpoint = "sendPhoto" if is_image else "sendDocument"
    field_name = "photo" if is_image else "document"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{bot_token}/{endpoint}",
                data={
                    "chat_id": chat_id,
                    "caption": f"{file_obj.get('field','')}: {name[:160]}",
                },
                files={field_name: (name, raw, ct)},
            )
            return r.status_code == 200
    except Exception as e:
        logger.warning(f"_send_file_to_telegram failed: {e}")
        return False


def _build_antworten_text_file(
    *, token_obj: AnfrageToken, antworten: dict, now_berlin: datetime,
) -> tuple[str, bytes]:
    """Baut den Drive-Text-File-Inhalt + Filename aus einer Submission.

    Filename: anfrage_YYYY-MM-DD_HH-MM-SS.txt — sortiert chronologisch
    im Drive-Ordner. Sekunden-Praezision reicht fuer Eindeutigkeit
    (gleichzeitige Submissions desselben Kunden im selben Sekunden-
    fenster sind praktisch ausgeschlossen).
    """
    filename = f"anfrage_{now_berlin.strftime('%Y-%m-%d_%H-%M-%S')}.txt"

    kunde = token_obj.kunde_name or token_obj.kunde_email or "Unbekannt"
    lines = [
        f"Anfrage von: {kunde}",
        f"E-Mail: {token_obj.kunde_email or '—'}",
        f"Eingegangen: {now_berlin.strftime('%Y-%m-%d %H:%M:%S')} Europe/Berlin",
    ]
    if token_obj.original_subject:
        lines.append(f"Original-Betreff: {token_obj.original_subject}")
    lines.append("")
    lines.append("Antworten")
    lines.append("─" * 40)
    for key, value in (antworten or {}).items():
        if not value:
            continue
        label = key.replace("_", " ").title()
        rendered = _format_value_plain(value)
        if "\n" in rendered:
            lines.append(f"{label}:")
            lines.append(rendered)
        else:
            lines.append(f"{label}: {rendered}")
        lines.append("")
    return filename, ("\n".join(lines) + "\n").encode("utf-8")


async def _save_submission_to_drive(
    *, tenant: Tenant, token_obj: AnfrageToken, antworten: dict,
    employee_id, now_berlin: datetime,
) -> str | None:
    """Schreibt Text-Datei + alle Form-Uploads in den Kunden-Drive-Ordner.

    Files bekommen den gleichen Timestamp-Praefix wie die Text-Datei,
    damit alle Artefakte einer Submission im Ordner zusammen stehen:

        anfrage_2026-05-18_15-30-45.txt
        anfrage_2026-05-18_15-30-45__foto_kueche.jpg
        anfrage_2026-05-18_15-30-45__skizze.pdf

    Returns: Drive-Folder-URL bei Erfolg (fuer Anzeige im Telegram-
    Push), None bei Fehler. Eigene Fehler werden geloggt — Drive ist
    Backup, DB-Response bleibt Source of Truth.
    """
    import base64 as _b64
    from core.integrations.google_drive import upload_file_to_kunde_folder

    kunde_name = token_obj.kunde_name or token_obj.kunde_email or "Unbekannt"
    txt_filename, txt_content = _build_antworten_text_file(
        token_obj=token_obj, antworten=antworten, now_berlin=now_berlin,
    )
    prefix = txt_filename.removesuffix(".txt")

    folder_url = None
    try:
        result = await upload_file_to_kunde_folder(
            tenant_id=tenant.id,
            kunde_name=kunde_name,
            file_bytes=txt_content,
            filename=txt_filename,
            mime_type="text/plain; charset=utf-8",
            employee_id=employee_id,
        )
        folder_url = result.get("kunde_folder_url")
        logger.info(
            f"Anfrage-Text in Drive abgelegt: tenant={tenant.slug} "
            f"kunde={kunde_name} file={txt_filename}"
        )
    except Exception as e:
        logger.warning(f"Drive-Save (Text) fehlgeschlagen: {e}")
        return None

    # Alle Form-Uploads (Fotos, PDFs) mit gleichem Praefix nachschieben.
    # Per-File-Try damit ein einzelner Fehler nicht den ganzen Batch killt.
    for f in _extract_files(antworten):
        try:
            raw = _b64.b64decode(f["base64"])
        except Exception as e:
            logger.warning(f"Drive-File-Decode fehlgeschlagen {f['filename']}: {e}")
            continue
        try:
            await upload_file_to_kunde_folder(
                tenant_id=tenant.id,
                kunde_name=kunde_name,
                file_bytes=raw,
                filename=f"{prefix}__{f['filename']}"[:200],
                mime_type=f["content_type"],
                employee_id=employee_id,
            )
        except Exception as e:
            logger.warning(
                f"Drive-Save (File {f['filename']}) fehlgeschlagen: {e}"
            )
    return folder_url


def _anliegen_text_from_antworten(antworten: dict) -> str:
    """Aggregiert die Freitext-Antworten zu einem Skill-Routing-Input.

    Listen/Dicts (z.B. Multi-Select, File-Uploads) werden ausgelassen —
    der Skill-Router matcht nur ueber Substring-Vergleich auf
    KEYWORD_TO_SKILL und braucht Freitext, nicht strukturierte Daten.
    """
    parts = []
    for value in (antworten or {}).values():
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " ".join(parts)


async def notify_tenant_anfrage_submitted(token_str: str, antworten: dict) -> None:
    """Verarbeitet eine Formular-Submission: Drive-Archiv + Telegram-Push.

    Routing-Quelle (in dieser Reihenfolge):
    1. `AnfrageToken.assigned_employee_id` — sticky, falls die Anfrage
       aus einer bereits zugewiesenen Mail-Conversation kommt.
    2. `choose_employee()` ueber den aggregierten Antwort-Text.

    Drive-Save passiert VOR dem Telegram-Push — so bleibt das Archiv
    vollstaendig auch wenn das Telegram-Routing scheitert (z.B.
    Mitarbeiter ohne aktivierte chat_id).
    """
    from plugins.telegram_notify.handler import TelegramNotifier
    from core.routing.employee_router import choose_employee

    async with AsyncSessionLocal() as session:
        token_obj = (await session.execute(
            select(AnfrageToken).where(AnfrageToken.token == token_str)
        )).scalar_one_or_none()
        if not token_obj:
            return

        tenant = (await session.execute(
            select(Tenant).where(Tenant.id == token_obj.tenant_id)
        )).scalar_one_or_none()
        if not tenant:
            return

    sticky_emp_id = getattr(token_obj, "assigned_employee_id", None)
    employee_id = sticky_emp_id
    if employee_id is None:
        try:
            routing = await choose_employee(
                tenant_id=tenant.id,
                anliegen_text=_anliegen_text_from_antworten(antworten),
            )
            employee_id = routing.employee_id if routing else None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"anfrage_telegram: choose_employee failed: {e}")

    now_berlin = datetime.now(_BERLIN_TZ)
    drive_folder_url = await _save_submission_to_drive(
        tenant=tenant, token_obj=token_obj, antworten=antworten,
        employee_id=employee_id, now_berlin=now_berlin,
    )

    bot_token, chat_id, prefix = await TelegramNotifier.resolve_employee_push_target(
        tenant.id, employee_id,
    )
    if not bot_token or not chat_id:
        logger.warning(
            f"Tenant {tenant.slug} hat kein Telegram-Ziel — anfrage-Push skip"
        )
        return

    kunde = token_obj.kunde_name or token_obj.kunde_email
    msg = (
        f"<b>Neue Anfrage von {_h(kunde)}</b>\n"
        f"<i>{_h(token_obj.kunde_email)}</i>\n\n"
    )
    if token_obj.original_subject:
        msg += f"<b>Original-Betreff:</b> {_h(token_obj.original_subject)}\n\n"

    msg += "<b>Antworten:</b>\n"
    for key, value in antworten.items():
        if not value:
            continue
        label = _h(key.replace("_", " ").title())
        msg += f"<b>{label}:</b> {_format_value(value)}\n"

    if drive_folder_url:
        msg += (
            f"\n📁 <a href=\"{_h(drive_folder_url)}\">"
            f"Alles im Drive-Ordner</a> — oder via /archiv "
            f"{_h(kunde or '')}"
        )
    msg += "\n\nMit /angebot kannst du jetzt direkt ein Lexware-Angebot anlegen."

    try:
        await TelegramNotifier._send_raw(bot_token, chat_id, f"{prefix}{msg}")
        logger.info(f"Anfrage-Push an Tenant {tenant.slug} gesendet")
    except Exception as e:
        logger.exception(f"Telegram-Push fehler: {e}")
        return

    # Files zusaetzlich an Telegram damit der Handwerker sie direkt
    # sehen kann ohne Drive zu oeffnen. Die liegen ja auch schon im
    # Drive-Ordner archiviert.
    try:
        files = _extract_files(antworten)
        for f in files:
            await _send_file_to_telegram(
                bot_token=bot_token, chat_id=chat_id, file_obj=f,
            )
    except Exception as e:
        logger.warning(f"Anfrage-Files-Forward failed (non-fatal): {e}")
