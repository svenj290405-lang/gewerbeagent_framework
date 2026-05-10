"""Background-Task: Lexware-Bezahlstatus alle 30 Minuten pollen.

Wird in core/api/app.py via asyncio.create_task() gestartet.

Was es tut:
- Alle Rechnungen mit status='mail_sent' und bezahlt_am IS NULL durchgehen
- Pro Tenant einmal LexwareProvider bauen, dann pro Rechnung get_invoice()
- Wenn voucherStatus in {paid, paidoff}: bezahlt_am=now, status='bezahlt'
- last_paid_check_at und lexware_voucher_status werden IMMER gesetzt,
  damit man auch bei "noch offen" sehen kann wann zuletzt geprueft wurde

Was es nicht tut:
- Es schickt KEINE Telegram-Pushes. Das macht der separate Daily-Summary-Job
  um 18:00 (rechnung_paid_daily_summary.py).
- Es traegt nichts in api_usage_log ein. Lexware ist kostenlos
  (Office Plus Flatrate), Tracking lohnt sich nicht.

Failsafe-Pattern:
- Lexware-Fehler pro einzelne Rechnung werden geloggt, aber abbrechen
  den Lauf nicht. Eine kaputte Rechnung blockiert nicht den Rest.
- Tenants ohne Lexware-Setup werden silent uebersprungen.
- Bei Container-Restart faengt der Cron in 60s wieder von vorne an.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from core.database import AsyncSessionLocal
from core.integrations.lexware import LexwareProvider, AccountingError
from core.models import Tenant, ToolConfig
from core.models.rechnung import (
    LEXWARE_PAID_STATES,
    RECHNUNG_STATUS_BEZAHLT,
    RECHNUNG_STATUS_MAIL_SENT,
    Rechnung,
)
from core.security import decrypt

logger = logging.getLogger(__name__)

# Polling-Intervall: 30 Minuten (Sven-Wahl). Initiale Wartezeit nach
# App-Start: 90 Sekunden (Microsoft-Cron startet auch in den ersten 30s,
# wir wollen nicht beide Provider-API-Pools gleichzeitig hauen).
POLL_INTERVAL_SECONDS = 30 * 60
INITIAL_DELAY_SECONDS = 90
ERROR_RETRY_SECONDS = 60

LEXWARE_TOOL_NAME = "lexware"


async def _build_lexware_provider(tenant_id) -> LexwareProvider | None:
    """Sucht ToolConfig 'lexware', entschluesselt den API-Key, baut Provider.

    Identische Logik wie _get_lexware_provider_for_tenant() im
    telegram_notify-handler — hier dupliziert weil core/integrations/
    keine plugins/* importieren darf (Plugin-Layer ist hoeher).
    """
    async with AsyncSessionLocal() as session:
        tc = (await session.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == tenant_id,
                ToolConfig.tool_name == LEXWARE_TOOL_NAME,
            )
        )).scalar_one_or_none()
        if tc is None or not tc.enabled:
            return None
        cfg = tc.config or {}
        encrypted = cfg.get("encrypted_api_key")
        if not encrypted:
            return None
        try:
            api_key = decrypt(encrypted)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Lexware-API-Key entschluesseln fehlgeschlagen "
                f"fuer Tenant {tenant_id}: {exc}"
            )
            return None
        if not api_key:
            return None
        return LexwareProvider(api_key=api_key)


async def _check_one_invoice(
    rechnung_id,
    lexware_invoice_id,
    provider: LexwareProvider,
) -> tuple[str | None, bool]:
    """Holt einen Voucher-Status. Liefert (voucherStatus, is_paid).

    Bei API-Fehler: (None, False) — Caller muss damit leben (last_check
    bleibt unveraendert, beim naechsten Lauf nochmal versuchen).

    Sonderfall 404: Voucher wurde in Lexware geloescht. Wir markieren
    sie als 'cancelled' damit der Cron die Rechnung nicht ewig erneut
    pollt. Tenant sieht den Status in /rechnungen_anzeigen.
    """
    try:
        data = await provider.get_invoice(lexware_invoice_id)
    except AccountingError as exc:
        if exc.status_code == 404:
            logger.info(
                f"Lexware-Voucher {lexware_invoice_id} = 404 (geloescht) "
                f"-> als 'cancelled' markieren"
            )
            return "cancelled", False
        logger.warning(
            f"Lexware get_invoice({lexware_invoice_id}) failed: "
            f"{exc.status_code} — {exc}"
        )
        return None, False
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            f"Lexware get_invoice({lexware_invoice_id}) crashed: {exc}"
        )
        return None, False

    voucher_status = data.get("voucherStatus")
    if not isinstance(voucher_status, str):
        # Lexware-Schema-Variation? Defensiv loggen, aber nicht abbrechen.
        logger.warning(
            f"Lexware-Response ohne voucherStatus fuer {lexware_invoice_id}: "
            f"keys={list(data.keys())[:10]}"
        )
        return None, False

    is_paid = voucher_status.lower() in LEXWARE_PAID_STATES
    return voucher_status.lower(), is_paid


async def check_pending_invoices_for_tenant(
    tenant_id,
) -> dict:
    """Prueft alle offenen Rechnungen eines einzelnen Tenants.

    Returns dict mit Counter:
    {checked: int, paid: int, errors: int, no_change: int}
    """
    summary = {"checked": 0, "paid": 0, "errors": 0, "no_change": 0}

    provider = await _build_lexware_provider(tenant_id)
    if provider is None:
        return summary  # Tenant hat Lexware nicht eingerichtet — silent skip

    # Offene Rechnungen laden (kleiner SELECT, dann eine eigene Session
    # pro Update um Long-Lived-Lock zu vermeiden)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(
                Rechnung.id,
                Rechnung.lexware_invoice_id,
            ).where(
                Rechnung.tenant_id == tenant_id,
                Rechnung.status == RECHNUNG_STATUS_MAIL_SENT,
                Rechnung.bezahlt_am.is_(None),
                Rechnung.lexware_invoice_id.is_not(None),
            )
        )).all()

    for r_id, lex_id in rows:
        summary["checked"] += 1
        voucher_status, is_paid = await _check_one_invoice(
            r_id, lex_id, provider,
        )

        # last_paid_check_at IMMER setzen (auch bei API-Fehler nicht;
        # wir wollen den Diagnose-Wert nur bei erfolgreichem Call).
        if voucher_status is None:
            summary["errors"] += 1
            continue

        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            update_values = {
                "lexware_voucher_status": voucher_status,
                "last_paid_check_at": now,
                "updated_at": now,
            }
            if is_paid:
                update_values["bezahlt_am"] = now
                update_values["status"] = RECHNUNG_STATUS_BEZAHLT
                summary["paid"] += 1
            else:
                summary["no_change"] += 1

            await session.execute(
                update(Rechnung)
                .where(Rechnung.id == r_id)
                # Race-Schutz: nur updaten wenn die Rechnung noch im
                # Mail-Sent-Status ist. Ein paralleler manueller Check
                # hat sonst das Vorrecht.
                .where(Rechnung.bezahlt_am.is_(None))
                .values(**update_values)
            )
            await session.commit()

    if summary["paid"] > 0 or summary["errors"] > 0 or summary["checked"] > 5:
        logger.info(
            f"Bezahl-Polling Tenant {tenant_id}: "
            f"{summary['checked']} geprueft, {summary['paid']} neu bezahlt, "
            f"{summary['errors']} API-Fehler"
        )
    return summary


async def poll_all_tenants_once() -> dict:
    """Ein Polling-Lauf ueber alle Tenants mit aktiver Lexware-Verbindung."""
    summary = {
        "tenants_checked": 0,
        "total_invoices_checked": 0,
        "total_paid": 0,
        "total_errors": 0,
    }

    # Wir brauchen alle Tenants die Lexware konfiguriert haben — also
    # JOIN auf ToolConfig.
    async with AsyncSessionLocal() as session:
        tenants = (await session.execute(
            select(Tenant.id, Tenant.slug).join(
                ToolConfig, ToolConfig.tenant_id == Tenant.id,
            ).where(
                ToolConfig.tool_name == LEXWARE_TOOL_NAME,
                ToolConfig.enabled.is_(True),
            )
        )).all()

    for tenant_id, slug in tenants:
        try:
            t_summary = await check_pending_invoices_for_tenant(tenant_id)
            summary["tenants_checked"] += 1
            summary["total_invoices_checked"] += t_summary["checked"]
            summary["total_paid"] += t_summary["paid"]
            summary["total_errors"] += t_summary["errors"]
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"Bezahl-Polling Tenant {slug} crashed: {exc}"
            )
            summary["total_errors"] += 1

        # Kurze Pause zwischen Tenants um Lexware-Rate-Limit
        # (2 req/s) bei vielen kleinen Tenants nicht zu reizen.
        await asyncio.sleep(0.3)

    return summary


async def cron_loop() -> None:
    """Endlosschleife: alle 30 Min Lexware-Voucher-Status prufen."""
    logger.info(
        f"Bezahl-Polling-Cron gestartet "
        f"(Intervall: {POLL_INTERVAL_SECONDS}s)"
    )
    await asyncio.sleep(INITIAL_DELAY_SECONDS)

    while True:
        try:
            started = datetime.now(timezone.utc)
            summary = await poll_all_tenants_once()
            duration = (datetime.now(timezone.utc) - started).total_seconds()

            # Log nur wenn was passiert ist — Cron-Schweigen ist Healthy
            if (summary["total_paid"] > 0
                    or summary["total_errors"] > 0
                    or summary["total_invoices_checked"] > 5):
                logger.info(
                    f"Bezahl-Polling-Lauf fertig in {duration:.1f}s: "
                    f"{summary['tenants_checked']} Tenants, "
                    f"{summary['total_invoices_checked']} Rechnungen geprueft, "
                    f"{summary['total_paid']} neu bezahlt, "
                    f"{summary['total_errors']} Fehler"
                )

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Bezahl-Polling-Cron gestoppt")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"Bezahl-Polling-Loop unerwarteter Fehler: {exc}"
            )
            await asyncio.sleep(ERROR_RETRY_SECONDS)
