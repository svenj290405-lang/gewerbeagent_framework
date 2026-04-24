"""Manifest fuer Kalender-Plugin."""
from core.plugin_system import PluginManifest

MANIFEST = PluginManifest(
    name="kalender",
    version="1.0.0",
    display_name="Google Kalender",
    description="Verbindung zu Google Calendar: Verfuegbarkeit pruefen und Termine buchen via Voice-AI.",
    required_oauth_scopes=[
        "https://www.googleapis.com/auth/calendar",
    ],
    default_config={
        "betrieb_name": "Mein Betrieb",
        "calendar_id": "primary",
        "arbeitszeiten_start": "08:00",
        "arbeitszeiten_ende": "17:00",
        "arbeitstage": [0, 1, 2, 3, 4],  # Mo-Fr (Mo=0, So=6)
        "termin_dauer_minuten": 90,
        "zeitzone": "Europe/Berlin",
    },
    webhook_endpoints=[
        {"path": "/check_availability", "method": "POST"},
        {"path": "/book_appointment", "method": "POST"},
    ],
)
