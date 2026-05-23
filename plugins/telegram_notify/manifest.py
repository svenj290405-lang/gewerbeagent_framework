"""Manifest fuer Telegram-Notification-Plugin."""
from core.plugin_system import PluginManifest

MANIFEST = PluginManifest(
    name="telegram_notify",
    version="1.0.0",
    display_name="Telegram Notifications",
    description=(
        "Sendet Push-Nachrichten an Telegram-Chats via Bot. "
        "Pro Tenant ein eigener Bot-Token + Chat-ID. "
        "Spaeter erweiterbar um Steuer-Befehle."
    ),
    default_config={
        "bot_token": "",        # Telegram-Bot-Token vom @BotFather
        "chat_id": "",          # Empfaenger-Chat-ID (User oder Gruppe)
        "betrieb_name": "",     # fuer schoene Nachricht
        "enabled": True,        # Plugin-weit aus/an
    },
    # Empfaengt echte externe Webhooks von Telegram (POST .../incoming) —
    # Auth per X-Telegram-Bot-Api-Secret-Token-Vergleich in on_webhook.
    external_webhook=True,
    webhook_endpoints=[
        # Reserviert fuer zukuenftige Befehl-Empfangs-Logik:
        # {"path": "/update", "method": "POST"},
    ],
)
