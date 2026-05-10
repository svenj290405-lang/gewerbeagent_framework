"""Telegram-Push wenn Kunde Anfrage-Formular abschickt."""
from __future__ import annotations

import logging
from html import escape as _h
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import AnfrageToken, Tenant

logger = logging.getLogger(__name__)


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


async def notify_tenant_anfrage_submitted(token_str: str, antworten: dict) -> None:
    """Schickt eine Telegram-Nachricht an den Tenant wenn jemand Anfrage abschickt."""
    from plugins.telegram_notify.handler import send_telegram_message

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AnfrageToken).where(AnfrageToken.token == token_str)
        )
        token_obj = result.scalar_one_or_none()
        if not token_obj:
            return

        t_result = await session.execute(
            select(Tenant).where(Tenant.id == token_obj.tenant_id)
        )
        tenant = t_result.scalar_one_or_none()
        if not tenant:
            return

    # Tenant-Telegram-Chat-ID laden
    chat_id = getattr(tenant, "telegram_chat_id", None)
    if not chat_id:
        logger.warning(f"Tenant {tenant.slug} hat keine telegram_chat_id, kein Push moeglich")
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

    try:
        await send_telegram_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        logger.info(f"Anfrage-Push an Tenant {tenant.slug} gesendet")
    except Exception as e:
        logger.exception(f"Telegram-Push fehler: {e}")
        return

    # Hochgeladene Dateien einzeln nachsenden (sendPhoto / sendDocument).
    # Failsafe — schluckt eigene Fehler, schickt niemals den ganzen Push down.
    try:
        files = _extract_files(antworten)
        if not files:
            return
        # bot_token holen analog send_telegram_message
        from core.models import ToolConfig
        async with AsyncSessionLocal() as session:
            tc = (await session.execute(
                select(ToolConfig)
                .join(Tenant, ToolConfig.tenant_id == Tenant.id)
                .where(
                    Tenant.slug == "_global",
                    ToolConfig.tool_name == "telegram_notify",
                )
            )).scalar_one_or_none()
        bot_token = (tc.config or {}).get("bot_token") if tc else None
        if not bot_token:
            return
        for f in files:
            await _send_file_to_telegram(
                bot_token=bot_token, chat_id=chat_id, file_obj=f,
            )
    except Exception as e:
        logger.warning(f"Anfrage-Files-Forward failed (non-fatal): {e}")
