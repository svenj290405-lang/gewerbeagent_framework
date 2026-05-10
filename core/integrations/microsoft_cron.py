"""Background-Task: alle Microsoft-Postfaecher regelmaessig pollen.

Wird in core/api/app.py ueber asyncio.create_task() gestartet.
Polled alle 2 Min alle Postfaecher (Tenant + Mitarbeiter) mit aktiver
Microsoft-Verbindung.

Phase 1 Multi-OAuth: iteriert ueber alle OAuthTokens (statt nur ueber
Tenants), damit jedes verbundene Mitarbeiter-Postfach sein eigenes
Polling bekommt.
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


async def get_microsoft_mailboxes() -> list[tuple[Tenant, OAuthToken]]:
    """Laedt alle (Tenant, OAuthToken) Paare mit Microsoft-Verbindung.

    Mit Multi-OAuth kann ein Tenant mehrere Tokens haben (1 pro
    Mitarbeiter). Jeder Token = ein Postfach das gepollt werden soll.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant, OAuthToken)
            .join(OAuthToken, OAuthToken.tenant_id == Tenant.id)
            .where(OAuthToken.provider == "microsoft")
        )
        return [(t, ot) for t, ot in result.all()]


async def poll_all_tenants_once() -> dict:
    """Ein Polling-Lauf fuer alle verbundenen Postfaecher."""
    mailboxes = await get_microsoft_mailboxes()
    logger.info(f"Cron-Polling: {len(mailboxes)} Microsoft-Postfaecher")

    summary = {"tenants_checked": 0, "total_mails": 0, "total_processed": 0, "errors": 0}

    for tenant, token in mailboxes:
        emp_id = token.employee_id
        label = f"{tenant.slug}{('/'+str(emp_id)[:8]) if emp_id else ''}"
        try:
            result = await poll_microsoft_inbox(tenant.id, employee_id=emp_id)
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
                    f"Cron-Postfach {label}: {n_checked} neue Mails, "
                    f"Verteilung: {result.get('classified', {})}"
                )
        except Exception as e:
            summary["errors"] += 1
            logger.exception(f"Cron-Polling-Fehler fuer {label}: {e}")

        # Pause zwischen Postfaechern um nicht alle gleichzeitig zu pollen
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
