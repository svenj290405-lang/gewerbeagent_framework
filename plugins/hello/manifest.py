"""Manifest fuer das Hello-Test-Plugin."""
from core.plugin_system import PluginManifest

MANIFEST = PluginManifest(
    name="hello",
    version="1.0.0",
    display_name="Hello World Test",
    description="Simples Test-Plugin um die Plugin-Architektur zu verifizieren",
    default_config={"greeting": "Hallo"},
    webhook_endpoints=[
        {"path": "/greet", "method": "POST"},
    ],
)
