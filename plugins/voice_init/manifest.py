"""
Manifest fuer voice_init Plugin.

Empfaengt Conversation-Initiation-Webhooks von ElevenLabs bei eingehenden
Anrufen. Liefert dynamische System-Prompt-Daten zurueck (Wissensbasis,
Firmenname etc.) damit der Voice-Agent tenant-spezifisch antworten kann.
"""
from core.plugin_system import PluginManifest

MANIFEST = PluginManifest(
    name="voice_init",
    version="1.0.0",
    display_name="Voice Initiation Webhook",
    description=(
        "ElevenLabs Conversation-Initiation-Webhook fuer Tenant-spezifische "
        "Prompts. Liefert Wissensbasis, Firmenname und Branche dynamisch."
    ),
    default_config={},
    # Empfaengt echte externe Webhooks von ElevenLabs — Auth per
    # X-Webhook-Secret-Vergleich in on_webhook (fail-closed in Production).
    external_webhook=True,
    webhook_endpoints=[
        {"path": "/initiation", "method": "POST"},
        {"path": "/save_contact", "method": "POST"},
        {"path": "/rueckruf_anfordern", "method": "POST"},
        {"path": "/checke_kalender", "method": "POST"},
        {"path": "/starte_terminsuche", "method": "POST"},
        {"path": "/hole_terminvorschlaege", "method": "POST"},
        {"path": "/buche_termin", "method": "POST"},
        {"path": "/finde_termine", "method": "POST"},
        {"path": "/storniere_termin", "method": "POST"},
        {"path": "/wissensbasis", "method": "POST"},
        {"path": "/call_ended", "method": "POST"},
    ],
)
