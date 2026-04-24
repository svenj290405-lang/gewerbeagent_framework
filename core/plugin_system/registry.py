"""
Plugin-Registry — Auto-Discovery und Instanziierung von Plugins.

Beim Framework-Start:
1. Scannt alle Unterordner in plugins/
2. Laedt von jedem ein manifest.py (muss MANIFEST exportieren)
3. Laedt handler.py (muss Plugin-Klasse erben von BasePlugin)
4. Registriert im PLUGIN_REGISTRY-Dict

Zur Laufzeit:
- get_plugin_for_tenant(tenant_id, plugin_name) laedt die ToolConfig
  und instanziiert das Plugin mit Tenant-spezifischem Context
"""
from __future__ import annotations

import importlib
import logging
import uuid
from pathlib import Path

from sqlalchemy import select

from config.settings import settings
from core.database import AsyncSessionLocal
from core.models import Tenant, ToolConfig
from core.plugin_system.base import BasePlugin, PluginContext, PluginManifest

logger = logging.getLogger(__name__)

# Plugin-Klasse pro Name (gefuellt beim Discovery)
PLUGIN_CLASSES: dict[str, type[BasePlugin]] = {}
PLUGIN_MANIFESTS: dict[str, PluginManifest] = {}


def discover_plugins() -> None:
    """Scannt plugins/ und laedt alle gueltigen Plugins."""
    plugins_dir = settings.project_root / "plugins"

    if not plugins_dir.exists():
        logger.warning(f"Plugins-Ordner existiert nicht: {plugins_dir}")
        return

    for plugin_path in plugins_dir.iterdir():
        if not plugin_path.is_dir():
            continue
        if plugin_path.name.startswith("_"):
            continue  # _template etc. ignorieren

        plugin_name = plugin_path.name
        try:
            _load_single_plugin(plugin_name)
            logger.info(f"Plugin geladen: {plugin_name}")
        except Exception as e:
            logger.error(f"Plugin {plugin_name} konnte nicht geladen werden: {e}")


def _load_single_plugin(plugin_name: str) -> None:
    """Laedt Manifest + Handler eines einzelnen Plugins."""
    manifest_module = importlib.import_module(f"plugins.{plugin_name}.manifest")
    handler_module = importlib.import_module(f"plugins.{plugin_name}.handler")

    manifest: PluginManifest = manifest_module.MANIFEST
    plugin_class: type[BasePlugin] = handler_module.Plugin

    if manifest.name != plugin_name:
        raise ValueError(
            f"Manifest-Name '{manifest.name}' != Ordnername '{plugin_name}'"
        )

    PLUGIN_CLASSES[plugin_name] = plugin_class
    PLUGIN_MANIFESTS[plugin_name] = manifest


async def get_plugin_for_tenant(
    tenant_slug: str, plugin_name: str
) -> BasePlugin | None:
    """
    Laedt Plugin fuer einen Tenant, falls aktiviert.

    Returns None wenn:
    - Tenant existiert nicht
    - Plugin existiert nicht
    - Plugin ist fuer diesen Tenant nicht aktiviert
    """
    if plugin_name not in PLUGIN_CLASSES:
        logger.warning(f"Plugin nicht gefunden: {plugin_name}")
        return None

    async with AsyncSessionLocal() as session:
        # Tenant holen
        result = await session.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )
        tenant = result.scalar_one_or_none()
        if not tenant:
            logger.warning(f"Tenant nicht gefunden: {tenant_slug}")
            return None

        # Tool-Config fuer diesen Plugin pruefen
        result = await session.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == tenant.id,
                ToolConfig.tool_name == plugin_name,
            )
        )
        tool_config = result.scalar_one_or_none()

        if not tool_config or not tool_config.enabled:
            logger.info(f"Plugin {plugin_name} fuer Tenant {tenant_slug} nicht aktiviert")
            return None

        # Config mergen: Manifest-Defaults + Tenant-Overrides
        manifest = PLUGIN_MANIFESTS[plugin_name]
        merged_config = {**manifest.default_config, **tool_config.config}

        context = PluginContext(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            config=merged_config,
        )

        plugin_class = PLUGIN_CLASSES[plugin_name]
        return plugin_class(context)