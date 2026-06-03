"""
Zentrale Konfiguration für das Gewerbeagent Framework.
"""
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Empfohlene Mindest-Laenge fuer SECRET_KEY/ENCRYPTION_KEY. Hart kann
# der min_length=32 bleiben (Backward-Compat fuer Bestands-Deployments
# mit 32-byte Keys = ~43 base64 chars). Aber wir warnen beim Boot wenn
# weniger als 64 chars (= ~48 bytes) — `openssl rand -base64 48`.
RECOMMENDED_KEY_LENGTH = 64


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    database_url: str = Field(
        default="postgresql+asyncpg://gewerbeagent:devpass@localhost:5432/gewerbeagent",
    )

    secret_key: str = Field(..., min_length=32)
    encryption_key: str = Field(..., min_length=32)

    google_application_credentials: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_location: str = "europe-west3"

    # Smart-Routing: Gemini waehlt anhand der Mitarbeiter-Skills den
    # passendsten Mitarbeiter (statt starrem Stichwort-Match). Failsafe:
    # bei Fehler/Timeout faellt der Router auf die Stichwort-Logik zurueck.
    # False = nur Stichwort-Logik, kein Gemini im Router.
    smart_routing_enabled: bool = True

    elevenlabs_api_key: str = ""

    # Google Maps Platform — Geocoding API + Distance Matrix API. Bevorzugter
    # Geo-Provider weil Sven schon ein GCP-Projekt fuer Vertex/Gemini hat;
    # ein zusaetzlicher API-Key im selben Projekt ist 1-Klick statt
    # 'neues OpenRouteService-Konto + Free-Tier-Quota nachhalten'.
    # Free-Tier: $200/Monat Google-Credit = ca. 40k Geocodes + 40k Distance-
    # Matrix-Eintraege im Monat. Reicht fuer Dutzende Tenants locker.
    # Setup-Anleitung: siehe .env.prod.example.
    google_maps_api_key: str = ""

    # OpenRouteService — Geocoding + Travel-Time-Matrix als Fallback wenn
    # kein Google-Maps-Key gesetzt ist. EU-hosted (Heidelberg), DSGVO-konform.
    # Free-Tier: 2.000 Requests/Tag. Wir behalten ihn als Backup damit
    # bestehende Installationen nicht brechen — neue Installs nehmen lieber
    # Google Maps.
    openrouteservice_api_key: str = ""

    admin_telegram_bot_token: str = ""
    admin_telegram_chat_id: str = ""

    # Webhook-Secret-Tokens fuer Signature-Verifikation eingehender Webhooks.
    # Leer = Verifikation deaktiviert (Backward-Compat fuer Legacy-Setups
    # ohne Secret). Sobald gesetzt, weist der Server alles ohne passenden
    # Header ab.
    #
    # Telegram: setzbar via setWebhook secret_token-Parameter; Telegram
    #   sendet ihn als 'X-Telegram-Bot-Api-Secret-Token' bei jedem Update.
    # ElevenLabs: HMAC-SHA256 signed via 'ElevenLabs-Signature'-Header
    #   wenn beim Webhook-Setup ein secret konfiguriert wurde.
    telegram_webhook_secret: str = ""
    elevenlabs_webhook_secret: str = ""

    public_url: str = "http://localhost:8000"

    # --- Inhaber-/Mitarbeiter-PWA (loest den Telegram-Bot als Oberflaeche ab) ---
    # Basis-URL der App (fuer Magic-Link-Mails + Service-Worker-Scope).
    # Leer = public_url verwenden. Prod: https://app.gewerbeagent.de
    app_base_url: str = ""

    # Web-Push (VAPID). Schluesselpaar via `python -m core.integrations.push_notifier
    # --genkey` erzeugen und in .env hinterlegen. Leer = Push deaktiviert
    # (App funktioniert, schickt nur keine Benachrichtigungen).
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    # 'sub'-Claim fuer VAPID — eine Kontakt-Mailadresse des Betreibers.
    vapid_subject: str = "mailto:datenschutz@gewerbeagent.de"

    @property
    def app_url(self) -> str:
        """Basis-URL der PWA — app_base_url falls gesetzt, sonst public_url."""
        return (self.app_base_url or self.public_url).rstrip("/")

    # Cron-Loops im Dev-Stack abschalten. Verhindert dass Dev-Stack echte
    # API-Quoten verbraucht (Gemini, Vertex), Test-Mails verschickt
    # (Brevo) oder Bezahl-Polls fuer Prod-Tenants ausloest. Standard:
    # auto an wenn environment != 'production'. Mit DEV_CRON_DISABLED=true
    # in .env.dev kann man auch im Dev explizit alle Crons abschalten.
    dev_cron_disabled: bool = False

    # Taeglicher System-Health-Check (core/integrations/daily_health_check.py).
    # Prueft morgens, ob DB/Telegram-Bot/Crons laufen, schreibt das Ergebnis
    # ins Admin-Tool (/admin/health) und schickt bei einem Problem eine Mail.
    health_check_enabled: bool = True
    health_check_hour: int = 7          # Stunde (Europe/Berlin), morgens
    health_alert_email: str = "svenj05@gmx.de"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def crons_enabled(self) -> bool:
        """True wenn Background-Cron-Loops gestartet werden sollen.

        Prod: immer ja. Dev: nur wenn dev_cron_disabled=False explizit.
        """
        if self.is_production:
            return True
        return not self.dev_cron_disabled

    @property
    def project_root(self) -> Path:
        return Path(__file__).parent.parent.resolve()


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # Sicherheits-Warnungen — keine Hard-Fails damit Bestands-Deployments
    # nicht beim Update brechen. Bei Phase-B Encryption-Key-Rotation wird
    # darauf geachtet.
    if len(s.encryption_key) < RECOMMENDED_KEY_LENGTH:
        logger.warning(
            "ENCRYPTION_KEY ist nur %d Zeichen lang. Empfohlen: %d+ "
            "(generiere via `openssl rand -base64 48`). Rotation wird "
            "in Phase B angegangen.",
            len(s.encryption_key), RECOMMENDED_KEY_LENGTH,
        )
    if len(s.secret_key) < RECOMMENDED_KEY_LENGTH:
        logger.warning(
            "SECRET_KEY ist nur %d Zeichen lang. Empfohlen: %d+.",
            len(s.secret_key), RECOMMENDED_KEY_LENGTH,
        )
    return s


settings = get_settings()
