"""End-to-End-Test fuer /rechnung_pruefen mit einer ENTWURF-Rechnung.

1. Legt in pilots Lexware eine Entwurf-Rechnung an (loeschbar, kein
   Rechtsdokument, verbraucht keine Rechnungsnummer).
2. Legt eine Rechnung-Zeile (status=mail_sent) an, die darauf zeigt.
3. Laesst check_pending_invoices_for_tenant laufen (= /rechnung_pruefen).
4. Zeigt das Ergebnis + den in die DB geschriebenen voucher_status.
5. Raeumt auf: Rechnung-Zeile loeschen + Lexware-Entwurf loeschen.

Aufruf (im Container):
    uv run python -m scripts.test_rechnung_pruefen_e2e
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

from sqlalchemy import delete, select

from core.database import AsyncSessionLocal
from core.integrations.accounting_base import InvoiceLineItem
from core.integrations.rechnung_payment_monitor import (
    _build_lexware_provider,
    check_pending_invoices_for_tenant,
)
from core.models import Tenant, ToolConfig
from core.models.rechnung import RECHNUNG_STATUS_MAIL_SENT, Rechnung


async def main() -> int:
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant)
            .join(ToolConfig, ToolConfig.tenant_id == Tenant.id)
            .where(
                Tenant.slug == "pilot",
                ToolConfig.tool_name == "lexware",
                ToolConfig.enabled.is_(True),
            )
        )).scalar_one_or_none()
    if tenant is None:
        print("FEHLER: pilot-Tenant mit Lexware nicht gefunden")
        return 2
    print(f"Betrieb: {tenant.slug} ({tenant.company_name})")

    provider = await _build_lexware_provider(tenant.id)
    if provider is None:
        print("FEHLER: Lexware-Provider nicht baubar")
        return 3

    rechnung_id = None
    invoice_id = None
    try:
        # 1) Entwurf-Rechnung in Lexware
        line_items = [InvoiceLineItem(
            name="Test-Position (Pruef-Test)",
            quantity=1,
            unit_name="Stueck",
            unit_price_gross=119.0,
            description="Automatischer Test /rechnung_pruefen — bitte ignorieren",
            tax_rate_percent=19,
        )]
        draft = await provider.create_invoice_draft(
            line_items=line_items,
            one_time_address={
                "name": "Sven Jantos", "city": "Trier", "countryCode": "DE",
            },
            title="TEST Rechnung-Pruefen",
            introduction="Testrechnung (Entwurf) — bitte ignorieren.",
            remark="Test",
            tax_type="gross",
            finalize=False,
        )
        invoice_id = draft.invoice_id
        print(f"1) Lexware-Entwurf angelegt: invoice_id={invoice_id}")

        # 2) Rechnung-Zeile (status=mail_sent)
        async with AsyncSessionLocal() as s:
            r = Rechnung(
                tenant_id=tenant.id,
                input_type="text",
                status=RECHNUNG_STATUS_MAIL_SENT,
                kunde_name="Sven Jantos",
                kunde_email="svenj05@gmx.de",
                leistung_titel="Pruef-Test",
                betrag_brutto_eur=Decimal("119.00"),
                lexware_invoice_id=invoice_id,
            )
            s.add(r)
            await s.commit()
            await s.refresh(r)
            rechnung_id = r.id
        print(f"2) Rechnung-Zeile angelegt: id={rechnung_id} status=mail_sent")

        # 3) Check (= /rechnung_pruefen)
        summary = await check_pending_invoices_for_tenant(tenant.id)
        print(f"3) check_pending_invoices_for_tenant -> {summary}")

        # 4) Was steht jetzt in der DB?
        async with AsyncSessionLocal() as s:
            r = (await s.execute(
                select(Rechnung).where(Rechnung.id == rechnung_id)
            )).scalar_one()
            print(
                f"4) DB nach Check: status={r.status} "
                f"voucher_status={r.lexware_voucher_status} "
                f"bezahlt_am={r.bezahlt_am} last_check={r.last_paid_check_at}"
            )
            db_voucher_status = r.lexware_voucher_status
            db_checked_at = r.last_paid_check_at

        ok = (
            summary["checked"] == 1
            and summary["errors"] == 0
            and db_voucher_status is not None
            and db_checked_at is not None
        )
        print(
            "\nERGEBNIS: " + (
                "OK ✓ — Check liest den Lexware-Status korrekt und "
                "schreibt ihn in die DB."
                if ok else
                "AUFFAELLIG ✗ — bitte Output oben pruefen."
            )
        )
        return 0 if ok else 6
    finally:
        # 5) Aufraeumen — egal was oben passiert ist
        if rechnung_id is not None:
            async with AsyncSessionLocal() as s:
                await s.execute(delete(Rechnung).where(Rechnung.id == rechnung_id))
                await s.commit()
        deleted = None
        if invoice_id is not None:
            try:
                deleted = await provider.delete_voucher(invoice_id)
            except Exception as e:  # noqa: BLE001
                print(f"5) Lexware-Entwurf NICHT geloescht ({e}) — "
                      f"bitte ggf. manuell in Lexware loeschen: {invoice_id}")
        print(f"5) Aufgeraeumt: Rechnung-Zeile entfernt, "
              f"Lexware-Entwurf geloescht={deleted}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
