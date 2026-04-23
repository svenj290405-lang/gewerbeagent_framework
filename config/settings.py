"""
Zentrale Konfiguration für das Gewerbeagent Framework.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    elevenlabs_api_key: str = ""

    admin_telegram_bot_token: str = ""
    admin_telegram_chat_id: str = ""

    public_url: str = "http://localhost:8000"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def project_root(self) -> Path:
        return Path(__file__).parent.parent.resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
