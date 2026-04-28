"""
Telegram-Plugin: Push-Notifications + Empfang von Telegram-Updates.

Public API fuer andere Plugins:
    await TelegramNotifier.send_for_tenant(tenant_id, text)

Webhook-Empfang:
    POST /webhook/_global/telegram_notify/incoming
    -> verarbeitet /start <slug> (QR-Code-Onboarding) und weitere Befehle.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, ToolConfig
from core.plugin_system import BasePlugin
from plugins.telegram_notify.manifest import MANIFEST

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
HTTP_TIMEOUT_SECONDS = 10.0
GLOBAL_TENANT_SLUG = "_global"
TELEGRAM_BOT_TOOL_NAME = "telegram_bot"


class TelegramNotifier:
    """Standalone-Helfer fuer Push-Notifications."""

    @staticmethod
    async def send_for_tenant(tenant_id: uuid.UUID, text: str) -> bool:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(ToolConfig).where(
                        ToolConfig.tenant_id == tenant_id,
                        ToolConfig.tool_name == "telegram_notify",
                    )
                )
                tc = result.scalar_one_or_none()
                if tc is None or not tc.enabled:
                    logger.info(f"Telegram fuer Tenant {tenant_id} nicht aktiviert, skip")
                    return False
                cfg = {**MANIFEST.default_config, **(tc.config or {})}
                bot_token = cfg.get("bot_token", "")
                chat_id = cfg.get("chat_id", "")
                if not bot_token or not chat_id:
                    logger.warning(f"Telegram-Config unvollstaendig fuer Tenant {tenant_id}")
                    return False
            return await TelegramNotifier._send_raw(bot_token, chat_id, text)
        except Exception as e:
            logger.exception(f"Telegram-Versand fehlgeschlagen: {e}")
            return False

    @staticmethod
    async def send_admin(bot_token: str, chat_id: str, text: str) -> bool:
        if not bot_token or not chat_id:
            return False
        try:
            return await TelegramNotifier._send_raw(bot_token, chat_id, text)
        except Exception as e:
            logger.exception(f"Admin-Telegram fehlgeschlagen: {e}")
            return False

    @staticmethod
    async def _send_raw(bot_token: str, chat_id: str, text: str) -> bool:
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(f"Telegram-API antwortete {resp.status_code}: {resp.text[:200]}")
                return False
            return True


# ===== INBOUND: Webhook-Empfang =====

async def _load_global_bot_token():
    async with AsyncSessionLocal() as s:
        global_tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == GLOBAL_TENANT_SLUG)
        )).scalar_one_or_none()
        if not global_tenant:
            logger.warning("_global Tenant fehlt")
            return None
        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == global_tenant.id,
                ToolConfig.tool_name == TELEGRAM_BOT_TOOL_NAME,
            )
        )).scalar_one_or_none()
        if not tc or not tc.enabled:
            logger.warning("telegram_bot ToolConfig fehlt")
            return None
        cfg = tc.config or {}
        return cfg.get("bot_token") or None


async def _send_to_chat(chat_id, text, bot_token=None):
    if bot_token is None:
        bot_token = await _load_global_bot_token()
        if bot_token is None:
            return False
    return await TelegramNotifier._send_raw(bot_token, str(chat_id), text)


async def _handle_start_command(text, chat_id, from_data):
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        msg_no_param = "Hallo! Dies ist der <b>Gewerbeagent-Bot</b>."
        msg_no_param += "\n\nFalls Sie sich gerade einrichten, scannen Sie bitte den QR-Code, "
        msg_no_param += "den Sie von uns erhalten haben - er enthaelt einen Aktivierungs-Link."
        return msg_no_param

    tenant_slug = parts[1].strip().lower()
    if not tenant_slug.replace("-", "").replace("_", "").isalnum():
        return "Aktivierungs-Link ungueltig. Bitte verwenden Sie den QR-Code."

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )).scalar_one_or_none()
        if tenant is None:
            return f"Aktivierungs-Link ungueltig (Tenant '{tenant_slug}' nicht gefunden)."
        if tenant_slug == GLOBAL_TENANT_SLUG:
            return "Dieser Aktivierungs-Link ist nicht fuer Endkunden bestimmt."
        if tenant.telegram_chat_id and tenant.telegram_chat_id != chat_id:
            logger.warning(f"Tenant {tenant_slug}: Chat-ID-Wechsel zu {chat_id}")
        tenant.telegram_chat_id = chat_id
        await s.commit()
        first_name = (from_data.get("first_name") or "").strip() or "dort"
        reply = f"Willkommen, {first_name}!\n\n"
        reply += f"Ihr Telegram ist jetzt mit <b>{tenant.company_name}</b> verbunden.\n\n"
        reply += "Ab jetzt erhalten Sie hier:\n"
        reply += "- Push-Nachrichten zu neuen Anrufen und Mails\n"
        reply += "- Bestaetigungen ueber gebuchte Termine\n"
        reply += "- Hinweise wenn Q nicht weiterkommt\n\n"
        reply += "Mit /help sehen Sie alle verfuegbaren Befehle."
        return reply


async def _handle_help_command():
    msg = "<b>Verfuegbare Befehle</b>\n\n"
    msg += "/start &lt;link&gt; - Bot mit Ihrem Betrieb verbinden (per QR-Code)\n"
    msg += "/status - Ist Ihr Agent aktiv?\n"
    msg += "/wissen - Wissensbasis pflegen (kommt bald)\n"
    msg += "/termine - Termine heute (kommt bald)\n"
    msg += "/help - Diese Liste\n\n"
    msg += "Bei Fragen: hallo@gewerbeagent.de"
    return msg


async def _handle_status_command(chat_id):
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.telegram_chat_id == chat_id)
        )).scalar_one_or_none()
        if not tenant:
            msg = "Dieser Chat ist noch <b>keinem Betrieb zugeordnet</b>.\n\n"
            msg += "Bitte scannen Sie den Aktivierungs-QR-Code."
            return msg
        return f"<b>{tenant.company_name}</b>\nStatus: {tenant.status}\nSlug: {tenant.slug}"


async def _handle_unknown():
    return "Diesen Befehl kenne ich noch nicht.\n\nMit /help sehen Sie was ich kann."


async def process_telegram_update(payload):
    msg = payload.get("message") or payload.get("edited_message")
    if not msg:
        logger.info("Telegram-Update ohne 'message' ignoriert")
        return {"ok": True}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_data = msg.get("from") or {}
    if not chat_id:
        logger.warning("Telegram-Update ohne chat_id")
        return {"ok": True}
    logger.info(f"Telegram in: chat_id={chat_id} text={text[:100]!r}")
    if text.startswith("/start"):
        reply = await _handle_start_command(text, chat_id, from_data)
    elif text == "/help":
        reply = await _handle_help_command()
    elif text == "/status":
        reply = await _handle_status_command(chat_id)
    elif text.startswith("/"):
        reply = await _handle_unknown()
    else:
        logger.info(f"Telegram Freitext ignoriert: {text[:100]}")
        return {"ok": True}
    await _send_to_chat(chat_id, reply)
    return {"ok": True}


class Plugin(BasePlugin):
    manifest = MANIFEST

    async def on_webhook(self, endpoint, payload):
        logger.info(f"Telegram-Webhook empfangen: endpoint={endpoint}")
        if endpoint == "incoming":
            return await process_telegram_update(payload)
        return {"ok": True, "note": f"unknown endpoint: {endpoint}"}
