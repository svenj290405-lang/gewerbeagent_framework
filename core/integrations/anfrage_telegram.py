"""Telegram-Push wenn Kunde Anfrage-Formular abschickt."""
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

    File-Antworten (Listen aus Dicts) werden zu '📎 N Datei(en)' kompakt
    — die Files selbst werden separat als sendPhoto/sendDocument
    gepusht (siehe notify_tenant_anfrage_submitted).
    """
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and v.get("filename") for v in value):
            return f"📎 {len(value)} Datei(en)"
        return _h(", ".join(str(v) for v in value if v))
    return _h(str(value or ""))


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


def _build_antworten_text_file(
    *, token_obj: AnfrageToken, antworten: dict, response_id,
) -> tuple[str, bytes]:
    """Baut den Drive-Text-File-Inhalt + Filename aus einer Submission.

    Filename: anfrage_YYYY-MM-DD_HH-MM_<sid>.txt — sortiert chronologisch
    im Drive-Ordner, der short_id-Suffix erlaubt eindeutige Zuordnung
    zur DB-Response auch wenn mehrere Anfragen pro Minute reinkommen.
    """
    from core.integrations.formular_eingang import short_id as _short_id

    now_berlin = datetime.now(_BERLIN_TZ)
    sid = _short_id(response_id)
    filename = f"anfrage_{now_berlin.strftime('%Y-%m-%d_%H-%M')}_{sid}.txt"

    kunde = token_obj.kunde_name or token_obj.kunde_email or "Unbekannt"
    lines = [
        f"Anfrage von: {kunde}",
        f"E-Mail: {token_obj.kunde_email or '—'}",
        f"Eingegangen: {now_berlin.strftime('%Y-%m-%d %H:%M')} Europe/Berlin",
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


async def _save_antworten_to_drive(
    *, tenant: Tenant, token_obj: AnfrageToken, antworten: dict,
    response_id, employee_id,
) -> None:
    """Best-Effort: schreibt die Antworten als Text-Datei in den
    Kunden-Drive-Ordner. Fehler werden geloggt aber nicht propagiert —
    der Telegram-Push und die DB-Response sind die Source of Truth,
    Drive ist ein zusaetzliches Archiv.
    """
    from core.integrations.google_drive import upload_file_to_kunde_folder

    kunde_name = token_obj.kunde_name or token_obj.kunde_email or "Unbekannt"
    filename, content = _build_antworten_text_file(
        token_obj=token_obj, antworten=antworten, response_id=response_id,
    )
    result = await upload_file_to_kunde_folder(
        tenant_id=tenant.id,
        kunde_name=kunde_name,
        file_bytes=content,
        filename=filename,
        mime_type="text/plain; charset=utf-8",
        employee_id=employee_id,
    )
    logger.info(
        f"Anfrage-Text in Drive abgelegt: tenant={tenant.slug} "
        f"kunde={kunde_name} file={filename} url={result.get('web_link')}"
    )


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
    """Schickt eine Telegram-Nachricht an den mail-zustaendigen Mitarbeiter
    wenn jemand das Anfrage-Formular abschickt.

    Routing-Quelle (in dieser Reihenfolge):
    1. `AnfrageToken.assigned_employee_id` — sticky, falls die Anfrage
       aus einer bereits zugewiesenen Mail-Conversation kommt.
    2. `choose_employee()` ueber den aggregierten Antwort-Text.

    Mitarbeiter ohne aktivierte telegram_chat_id loesen `[unzugewiesen
    fuer NAME]`-Praefix-Fallback an den Default-Employee aus
    (siehe TelegramNotifier.send_for_employee).
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

    # Skill-Routing: bevorzugt sticky aus Conversation, sonst skill-match.
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

    # Drive-Archiv: Antworten als Text-Datei in den Kunden-Ordner ablegen.
    # Laeuft VOR der Telegram-Push, damit ein Telegram-Routing-Fehler den
    # Drive-Save nicht ueberspringt. Failsafe — eigene Fehler schluckt.
    from core.integrations.formular_eingang import get_response_for_token
    response_pair = await get_response_for_token(token_str)
    try:
        if response_pair:
            response, _ = response_pair
            await _save_antworten_to_drive(
                tenant=tenant, token_obj=token_obj, antworten=antworten,
                response_id=response.id, employee_id=employee_id,
            )
    except Exception as e:
        logger.warning(
            f"Anfrage-Drive-Save fehlgeschlagen (nicht kritisch): {e}"
        )

    # Push-Ziel + Aktivierungs-Praefix aufloesen.
    bot_token, chat_id, prefix = await TelegramNotifier.resolve_employee_push_target(
        tenant.id, employee_id,
    )
    if not bot_token or not chat_id:
        logger.warning(
            f"Tenant {tenant.slug} hat kein Telegram-Ziel — anfrage-Push skip"
        )
        return

    # Nachricht bauen — alle Kunden-Inputs HTML-escapen vor f-String-Build
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
        # Snake_case lesbarer machen — key kommt aus Schema, Label safe;
        # value via _format_value escaped
        label = _h(key.replace("_", " ").title())
        msg += f"<b>{label}:</b> {_format_value(value)}\n"

    msg += "\nMit /angebot kannst du jetzt direkt ein Lexware-Angebot anlegen."

    # Inline-Status-Buttons: response_pair wurde oben schon fuer den
    # Drive-Save geladen. Wenn keine Response existiert (warum auch
    # immer), wird der Push trotzdem gesendet — nur ohne Buttons.
    from core.integrations.formular_eingang import short_id as _short_id
    keyboard = None
    if response_pair:
        response, _ = response_pair
        sid = _short_id(response.id)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📝 In Bearbeitung",
                     "callback_data": f"formeing:status:in_bearbeitung:{sid}"},
                ],
                [
                    {"text": "✅ Erledigt",
                     "callback_data": f"formeing:status:erledigt:{sid}"},
                    {"text": "❌ Abgelehnt",
                     "callback_data": f"formeing:status:abgelehnt:{sid}"},
                ],
            ],
        }

    try:
        if keyboard:
            from plugins.telegram_notify.handler import _send_with_keyboard
            await _send_with_keyboard(
                chat_id, f"{prefix}{msg}", keyboard, bot_token,
            )
        else:
            await TelegramNotifier._send_raw(bot_token, chat_id, f"{prefix}{msg}")
        logger.info(f"Anfrage-Push an Tenant {tenant.slug} gesendet")
    except Exception as e:
        logger.exception(f"Telegram-Push fehler: {e}")
        return

    # Hochgeladene Dateien einzeln nachsenden (sendPhoto / sendDocument)
    # an dieselbe Chat-ID. Failsafe — schluckt eigene Fehler, schickt
    # niemals den ganzen Push down.
    try:
        files = _extract_files(antworten)
        if not files:
            return
        for f in files:
            await _send_file_to_telegram(
                bot_token=bot_token, chat_id=chat_id, file_obj=f,
            )
    except Exception as e:
        logger.warning(f"Anfrage-Files-Forward failed (non-fatal): {e}")
