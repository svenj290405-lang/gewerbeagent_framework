"""Manifest fuer Mail-Intake-Plugin."""
from core.plugin_system import PluginManifest

MANIFEST = PluginManifest(
    name="mail_intake",
    version="1.0.0",
    display_name="Mail Intake (Brevo)",
    description=(
        "Empfaengt eingehende E-Mails ueber Brevo Inbound Parsing, "
        "extrahiert Termin-Wuensche per Gemini, bucht ggf. ueber kalender-Plugin, "
        "schickt Auto-Reply an Kunden + Telegram-Push an Tenant. "
        "Tenant-Routing ueber to-Adresse: dietz@reply.gewerbeagent.de -> tenant 'dietz'."
    ),
    default_config={
        # _global Tenant haelt diese Werte
        "brevo_api_key": "",
        "sender_name": "Gewerbeagent",
        "sender_email": "noreply@gewerbeagent.de",
        "inbound_domain": "reply.gewerbeagent.de",
        "enabled": True,
    },
    webhook_endpoints=[
        {"path": "/incoming", "method": "POST"},
    ],
)
