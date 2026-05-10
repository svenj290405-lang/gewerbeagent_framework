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
    """
    if isinstance(value, list):
        return _h(", ".join(str(v) for v in value if v))
    return _h(str(value or ""))


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
