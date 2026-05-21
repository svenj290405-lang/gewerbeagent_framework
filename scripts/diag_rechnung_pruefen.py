"""Read-only-Diagnose fuer /rechnung_pruefen (Lexware-Bezahl-Status-Check).

Geht alle Tenants mit aktivem Lexware durch, listet offene Rechnungen
(status=mail_sent, bezahlt_am IS NULL) und fragt fuer JEDE den echten
Lexware-voucherStatus ab (get_invoice) — OHNE etwas in die DB zu schreiben.
Zeigt damit, ob der Check korrekt funktionieren WUERDE: Verbindung,
Key-Entschluesselung, Status-Parsing, paid-Erkennung, Fehler.

Aufruf (im Container):
    uv run python -m scripts.diag_rechnung_pruefen
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

from core.database import AsyncSessionLocal
from core.integrations.rechnung_payment_monitor import (
    _build_lexware_provider,
    _check_one_invoice,
)
from core.models import Tenant, ToolConfig
from core.models.rechnung import RECHNUNG_STATUS_MAIL_SENT, Rechnung

LEXWARE = "lexware"


async def main() -> int:
    async with AsyncSessionLocal() as s:
        tenants = (await s.execute(
            select(Tenant.id, Tenant.slug, Tenant.company_name)
            .join(ToolConfig, ToolConfig.tenant_id == Tenant.id)
            .where(ToolConfig.tool_name == LEXWARE, ToolConfig.enabled.is_(True))
        )).all()

    if not tenants:
        print("Kein Tenant mit aktivem Lexware gefunden — /rechnung_pruefen "
              "wuerde ueberall still 'Keine offenen Rechnungen' melden.")
        return 0

    print(f"Tenants mit aktivem Lexware: {len(tenants)}\n")

    for tid, slug, company in tenants:
        print(f"=== {slug} ({company}) ===")
        async with AsyncSessionLocal() as s:
            dist = (await s.execute(
                select(Rechnung.status, func.count())
                .where(Rechnung.tenant_id == tid)
                .group_by(Rechnung.status)
            )).all()
        print("  Rechnungen nach Status: "
              + (", ".join(f"{st}={n}" for st, n in dist) or "keine"))

        provider = await _build_lexware_provider(tid)
        if provider is None:
            print("  WARNUNG: Lexware-Provider NICHT baubar (Key fehlt/kaputt?) "
                  "-> /rechnung_pruefen wuerde hier still nichts tun.\n")
            continue
        print("  OK: Lexware-Provider gebaut (API-Key entschluesselt).")

        async with AsyncSessionLocal() as s:
            rows = (await s.execute(
                select(
                    Rechnung.id, Rechnung.lexware_invoice_id,
                    Rechnung.kunde_name, Rechnung.betrag_brutto_eur,
                ).where(
                    Rechnung.tenant_id == tid,
                    Rechnung.status == RECHNUNG_STATUS_MAIL_SENT,
                    Rechnung.bezahlt_am.is_(None),
                    Rechnung.lexware_invoice_id.is_not(None),
                )
            )).all()

        if not rows:
            print("  Keine offenen Rechnungen (mail_sent + unbezahlt) "
                  "-> nichts zu pruefen.\n")
            continue

        print(f"  Offene Rechnungen: {len(rows)} — frage Lexware ab (read-only):")
        n_paid = n_open = n_err = 0
        for r_id, lex_id, kunde, betrag in rows:
            vs, is_paid = await _check_one_invoice(r_id, lex_id, provider)
            if vs is None:
                n_err += 1
                verdict = "API-FEHLER (None)"
            elif is_paid:
                n_paid += 1
                verdict = "WUERDE als BEZAHLT markiert"
            else:
                n_open += 1
                verdict = "offen (unveraendert)"
            print(f"    - {kunde or '?'} | {betrag}EUR | "
                  f"voucherStatus={vs} -> {verdict}")
        print(f"  Summe: {n_paid} bezahlt, {n_open} offen, {n_err} Fehler\n")

    print("Fertig — read-only, nichts in der DB geaendert.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
