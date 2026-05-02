"""
voice_init Plugin: Conversation-Initiation-Webhook fuer ElevenLabs.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import (
    ALLE_KATEGORIEN,
    KATEGORIE_LABELS,
    Tenant,
    TenantKnowledge,
)
from core.plugin_system import BasePlugin
from plugins.voice_init.manifest import MANIFEST

logger = logging.getLogger(__name__)


def _normalize_phone(num):
    """Bringt eine Telefonnummer in einheitliches Format (+49...).
    ElevenLabs koennte mit oder ohne + senden, mit/ohne Leerzeichen etc."""
    if not num:
        return None
    s = str(num).strip().replace(" ", "").replace("-", "")
    if s.startswith("+"):
        return s
    if s.startswith("00"):
        return "+" + s[2:]
    if s.startswith("0"):
        # Vermutlich deutsche Nummer ohne Laendercode
        return "+49" + s[1:]
    if s.isdigit():
        return "+" + s
    return s


async def _find_tenant_by_phone(phone_number):
    """Findet Tenant anhand der angerufenen Nummer."""
    normalized = _normalize_phone(phone_number)
    if not normalized:
        return None
    async with AsyncSessionLocal() as s:
        t = (await s.execute(
            select(Tenant).where(Tenant.voice_phone_number == normalized)
        )).scalar_one_or_none()
        if t:
            s.expunge(t)
        return t


async def _load_knowledge(tenant_id):
    """Holt alle Wissens-Eintraege eines Tenants, gruppiert nach Kategorie."""
    async with AsyncSessionLocal() as s:
        entries = (await s.execute(
            select(TenantKnowledge)
            .where(TenantKnowledge.tenant_id == tenant_id)
            .order_by(TenantKnowledge.kategorie, TenantKnowledge.created_at)
        )).scalars().all()
    by_kat = {}
    for e in entries:
        by_kat.setdefault(e.kategorie, []).append(e.text)
    return by_kat


def _build_knowledge_block(by_kat):
    """Baut einen lesbaren Wissens-Block fuer den System-Prompt."""
    if not by_kat:
        return "Es liegen noch keine spezifischen Betriebs-Informationen vor."
    parts = []
    for kat in ALLE_KATEGORIEN:
        if kat not in by_kat:
            continue
        label = KATEGORIE_LABELS.get(kat, kat)
        parts.append(f"## {label}")
        for text in by_kat[kat]:
            parts.append(f"- {text}")
        parts.append("")
    return "\n".join(parts).strip()


class Plugin(BasePlugin):
    """voice_init Plugin: liefert Init-Daten fuer ElevenLabs-Conversations."""

    manifest = MANIFEST

    async def on_webhook(self, endpoint, payload):
        if endpoint == "initiation":
            return await self._handle_initiation(payload)
        return {"error": f"Unbekannter Endpunkt: {endpoint}"}

    async def _handle_initiation(self, payload):
        """
        Wird von ElevenLabs bei jedem eingehenden Anruf aufgerufen.

        Erwartet (vereinfacht):
          { "caller_id": "...", "called_number": "+49...", "agent_id": "..." }
        oder via SIP-Trunk:
          { "call_sid": "...", "to": "+49...", "from": "..." }

        Gibt zurueck:
          {
            "type": "conversation_initiation_client_data",
            "dynamic_variables": {
              "tenant_company_name": "...",
              "tenant_branche": "...",
              "tenant_knowledge_block": "..."
            }
          }
        """
        # called_number aus den verschiedenen moeglichen Feldern lesen
        called = (
            payload.get("called_number")
            or payload.get("to_number")
            or payload.get("to")
            or payload.get("destination_number")
        )
        caller = (
            payload.get("caller_id")
            or payload.get("from_number")
            or payload.get("from")
            or payload.get("caller_number")
            or "unbekannt"
        )

        logger.info(
            f"voice_init: called={called!r} caller={caller!r}"
        )

        tenant = await _find_tenant_by_phone(called) if called else None

        if tenant is None:
            logger.warning(
                f"voice_init: Kein Tenant fuer called={called!r} gefunden, fallback"
            )
            return {
                "type": "conversation_initiation_client_data",
                "dynamic_variables": {
                    "tenant_company_name": "diesem Handwerksbetrieb",
                    "tenant_branche": "Handwerk",
                    "tenant_knowledge_block": "Es liegen aktuell keine spezifischen Informationen ueber den Betrieb vor.",
                },
            }

        by_kat = await _load_knowledge(tenant.id)
        knowledge_block = _build_knowledge_block(by_kat)

        logger.info(
            f"voice_init: Tenant={tenant.slug} branche={tenant.branche} "
            f"knowledge_entries={sum(len(v) for v in by_kat.values())}"
        )

        return {
            "type": "conversation_initiation_client_data",
            "dynamic_variables": {
                "tenant_company_name": tenant.company_name or "",
                "tenant_branche": tenant.branche or "Handwerk",
                "tenant_knowledge_block": knowledge_block,
            },
        }
