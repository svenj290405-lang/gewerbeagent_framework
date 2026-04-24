"""Handler des Hello-Plugins."""
from typing import Any

from core.plugin_system import BasePlugin
from plugins.hello.manifest import MANIFEST


class Plugin(BasePlugin):
    manifest = MANIFEST

    async def on_webhook(
        self, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if endpoint == "greet":
            name = payload.get("name", "unknown")
            greeting = self.config.get("greeting", "Hello")
            return {
                "message": f"{greeting}, {name}! Gesendet von Tenant {self.context.tenant_slug}.",
                "plugin_version": self.manifest.version,
            }
        return {"error": f"Unbekannter Endpunkt: {endpoint}"}
