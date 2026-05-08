"""Background-Task: alle Microsoft-Tenants regelmaessig pollen.

Wird in core/api/app.py ueber asyncio.create_task() gestartet.
Polled alle 2 Min alle Tenants mit aktiver Microsoft-Verbindung.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.integrations.microsoft_inbox import poll_microsoft_inbox
from core.models import OAuthToken, Tenant

logger = logging.getLogger(__name__)

# Konfiguration
POLL_INTERVAL_SECONDS = 120  # 2 Minuten
ERROR_RETRY_SECONDS = 60     # Bei Fehler kuerzer warten


async def get_microsoft_tenants() -> list[Tenant]:
    """Laedt alle Tenants die Microsoft verbunden haben."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant)
            .join(OAuthToken, OAuthToken.tenant_id == Tenant.id)
            .where(OAuthToken.provider == "microsoft")
        )
        return list(result.scalars().all())


async def poll_all_tenants_once() -> dict:
    """Ein Polling-Lauf fuer alle verbundenen Tenants."""
    tenants = await get_microsoft_tenants()
    logger.info(f"Cron-Polling: {len(tenants)} Tenants mit Microsoft-Verbindung")

    summary = {"tenants_checked": 0, "total_mails": 0, "total_processed": 0, "errors": 0}

    for tenant in tenants:
        try:
            result = await poll_microsoft_inbox(tenant.id)
            summary["tenants_checked"] += 1
            n_checked = result.get("checked", 0)
            summary["total_mails"] += n_checked

            # Wieviele wurden tatsaechlich beantwortet
            for m in result.get("messages", []):
                pr = m.get("process_result")
                if pr and pr.get("success"):
                    summary["total_processed"] += 1

            if n_checked > 0:
                logger.info(
                    f"Cron-Tenant {tenant.slug}: {n_checked} neue Mails, "
                    f"Verteilung: {result.get('classified', {})}"
                )
        except Exception as e:
            summary["errors"] += 1
            logger.exception(f"Cron-Polling-Fehler fuer Tenant {tenant.slug}: {e}")

        # Kleine Pause zwischen Tenants um nicht alle gleichzeitig auf Microsoft zu hauen
        await asyncio.sleep(0.5)

    return summary


async def cron_loop() -> None:
    """Endlosschleife: alle X Sek alle Tenants pollen."""
    logger.info(
        f"Microsoft-Cron gestartet (Intervall: {POLL_INTERVAL_SECONDS}s)"
    )
    # Initial 30 Sek warten damit App komplett gestartet ist
    await asyncio.sleep(30)

    while True:
        try:
            started = datetime.now(timezone.utc)
            summary = await poll_all_tenants_once()
            duration = (datetime.now(timezone.utc) - started).total_seconds()

            if summary["total_mails"] > 0 or summary["errors"] > 0:
                logger.info(
                    f"Cron-Lauf fertig in {duration:.1f}s: "
                    f"{summary['tenants_checked']} Tenants, "
                    f"{summary['total_mails']} Mails, "
                    f"{summary['total_processed']} verarbeitet, "
                    f"{summary['errors']} Fehler"
                )

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Microsoft-Cron gestoppt")
            raise
        except Exception as e:
            logger.exception(f"Cron-Loop unerwarteter Fehler: {e}")
            await asyncio.sleep(ERROR_RETRY_SECONDS)
