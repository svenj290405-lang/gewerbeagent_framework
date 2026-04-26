"""
Telegram-Notification-Plugin.

Public API fuer andere Plugins:
    await TelegramNotifier.send_for_tenant(tenant_id, text)

Pro Tenant wird die telegram_notify-ToolConfig geladen (Bot-Token, Chat-ID).
Falls deaktiviert oder fehlerhaft konfiguriert: silent skip + Log.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import ToolConfig
from core.plugin_system import BasePlugin
from plugins.telegram_notify.manifest import MANIFEST

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
HTTP_TIMEOUT_SECONDS = 10.0


class TelegramNotifier:
    """Standalone-Helfer: andere Plugins koennen das direkt aufrufen."""

    @staticmethod
    async def send_for_tenant(tenant_id: uuid.UUID, text: str) -> bool:
        """
        Laedt Telegram-Config fuer Tenant aus DB, sendet Nachricht.

        Returns:
            True bei Erfolg, False bei jeder Art von Fehler (silent fail).
            Loggt Probleme, wirft aber keine Exception nach oben - 
            Notifications sollen niemals den eigentlichen Workflow blocken.
        """
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
                    logger.info(
                        f"Telegram fuer Tenant {tenant_id} nicht aktiviert, skip"
                    )
                    return False

                cfg = {**MANIFEST.default_config, **(tc.config or {})}
                bot_token = cfg.get("bot_token", "")
                chat_id = cfg.get("chat_id", "")

                if not bot_token or not chat_id:
                    logger.warning(
                        f"Telegram-Config unvollstaendig fuer Tenant {tenant_id}"
                    )
                    return False

            return await TelegramNotifier._send_raw(bot_token, chat_id, text)

        except Exception as e:
            logger.exception(f"Telegram-Versand fehlgeschlagen: {e}")
            return False

    @staticmethod
    async def send_admin(bot_token: str, chat_id: str, text: str) -> bool:
        """
        Direkter Versand ohne DB - fuer Admin-Notifications an Sven.
        Token+ChatID kommen aus settings (.env).
        """
        if not bot_token or not chat_id:
            return False
        try:
            return await TelegramNotifier._send_raw(bot_token, chat_id, text)
        except Exception as e:
            logger.exception(f"Admin-Telegram fehlgeschlagen: {e}")
            return False

    @staticmethod
    async def _send_raw(bot_token: str, chat_id: str, text: str) -> bool:
        """Eigentlicher HTTP-Call an Telegram-API."""
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
                logger.warning(
                    f"Telegram-API antwortete {resp.status_code}: {resp.text[:200]}"
                )
                return False
            return True


class Plugin(BasePlugin):
    """Plugin-Klasse fuer das Framework. Aktuell nur Stub fuer kuenftige Befehle."""

    manifest = MANIFEST

    async def on_webhook(
        self, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        # Reserviert fuer Befehls-Empfang spaeter (Telegram-Update-Webhook).
        # Heute erstmal nur Logging.
        logger.info(f"Telegram-Webhook empfangen: endpoint={endpoint}")
        return {"ok": True, "note": "command-handling not yet implemented"}
