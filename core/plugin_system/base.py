"""
Plugin-System Basisklasse.

Jedes Plugin erbt von BasePlugin und implementiert die relevanten
Lifecycle-Methoden. Das Framework laedt, konfiguriert und dispatched
Plugins automatisch basierend auf der ToolConfig-Tabelle.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class PluginManifest(BaseModel):
    """Metadaten eines Plugins (statisch, beim Laden eingelesen)."""

    name: str  # eindeutig, entspricht Ordnername (z.B. "kalender")
    version: str  # Semver, z.B. "1.0.0"
    display_name: str  # fuer UI: "Google Kalender"
    description: str  # kurze Beschreibung

    # OAuth-Scopes die das Plugin braucht
    required_oauth_scopes: list[str] = []

    # JSON-Schema fuer die tenant-spezifische config
    config_schema: dict = {}

    # Default-Werte fuer config (werden mit Tenant-Config gemergt)
    default_config: dict = {}

    # Welche Webhook-Endpunkte das Plugin registriert
    # Format: [{"path": "/check_availability", "method": "POST"}, ...]
    webhook_endpoints: list[dict] = []


class PluginContext(BaseModel):
    """Context der an jeden Plugin-Aufruf uebergeben wird."""

    tenant_id: uuid.UUID
    tenant_slug: str
    config: dict  # gemergte Plugin-Config fuer diesen Tenant

    model_config = {"arbitrary_types_allowed": True}


class BasePlugin(ABC):
    """Basisklasse fuer alle Plugins."""

    manifest: PluginManifest  # muss von Subklasse gesetzt werden

    def __init__(self, context: PluginContext) -> None:
        self.context = context

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.context.tenant_id

    @property
    def config(self) -> dict:
        return self.context.config

    @abstractmethod
    async def on_webhook(
        self, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Wird aufgerufen wenn ein Webhook an dieses Plugin kommt.

        Args:
            endpoint: Welcher Endpunkt wurde aufgerufen (z.B. "check_availability")
            payload: JSON-Body des Requests

        Returns:
            Dict das als JSON-Response zurueckgeschickt wird
        """
        ...

    async def on_activate(self) -> None:
        """Wird aufgerufen wenn Plugin fuer Tenant aktiviert wird."""
        pass

    async def on_deactivate(self) -> None:
        """Wird aufgerufen wenn Plugin fuer Tenant deaktiviert wird."""
        pass