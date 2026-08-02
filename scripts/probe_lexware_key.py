"""Sondiert, was der Lexware-API-Key eines Betriebs tatsaechlich darf.

NUR Lese-Anfragen (GET). Legt nichts an, aendert nichts, loescht nichts.
Der Key selbst wird nie ausgegeben — nur, welche Endpunkte er oeffnet.

    docker exec -w /app -e PYTHONPATH=/app gewerbeagent_framework \
        /app/.venv/bin/python scripts/probe_lexware_key.py pilot

Hintergrund: die App nutzt heute nur contacts / vouchers / invoices /
quotations. Lexware Office kann deutlich mehr — welche Ressourcen der
konkrete Key freigibt, sieht man aber erst, wenn man sie anfragt
(403 = nicht erlaubt, 404 = gibt es nicht, 200/406 = erreichbar).
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

from core.integrations.lexware import LEXWARE_API_BASE
from core.integrations.rechnung_payment_monitor import _build_lexware_provider
from core.models.tenant import Tenant
from core.database.connection import get_session
from sqlalchemy import select

# (Pfad, Beschreibung) — alles rein lesend.
PROBES: list[tuple[str, str]] = [
    ("/v1/profile", "Firmenprofil (Betrieb, Steuerart, Kleinunternehmer)"),
    ("/v1/countries", "Laenderliste"),
    ("/v1/payment-conditions", "Zahlungsbedingungen (Zahlungsziel, Skonto)"),
    ("/v1/posting-categories", "Buchungskategorien (Kostenarten)"),
    ("/v1/print-layouts", "Druck-Layouts"),
    ("/v1/event-subscriptions", "Webhooks / Event-Abos"),
    ("/v1/recurring-templates?page=0&size=1", "Wiederkehrende Rechnungen (Vorlagen)"),
    ("/v1/contacts?page=0&size=1", "Kontakte"),
    ("/v1/voucherlist?voucherType=invoice&voucherStatus=open&page=0&size=1",
     "Belegliste: offene Rechnungen"),
    ("/v1/voucherlist?voucherType=invoice&voucherStatus=paid&page=0&size=1",
     "Belegliste: bezahlte Rechnungen"),
    ("/v1/voucherlist?voucherType=invoice&voucherStatus=overdue&page=0&size=1",
     "Belegliste: UEBERFAELLIGE Rechnungen (Lexware rechnet selbst)"),
    ("/v1/voucherlist?voucherType=purchaseinvoice&voucherStatus=open&page=0&size=1",
     "Belegliste: Eingangsrechnungen = AUSGABEN"),
    ("/v1/voucherlist?voucherType=quotation&voucherStatus=open&page=0&size=1",
     "Belegliste: offene Angebote"),
    ("/v1/voucherlist?voucherType=orderconfirmation&voucherStatus=open&page=0&size=1",
     "Belegliste: Auftragsbestaetigungen"),
    ("/v1/voucherlist?voucherType=deliverynote&voucherStatus=open&page=0&size=1",
     "Belegliste: Lieferscheine"),
    ("/v1/voucherlist?voucherType=creditnote&voucherStatus=open&page=0&size=1",
     "Belegliste: Gutschriften"),
    ("/v1/voucherlist?voucherType=downpaymentinvoice&voucherStatus=open&page=0&size=1",
     "Belegliste: Abschlagsrechnungen"),
    ("/v1/voucherlist?voucherType=dunning&voucherStatus=open&page=0&size=1",
     "Belegliste: Mahnungen"),
]


async def main(slug: str) -> None:
    async with get_session() as s:
        t = (await s.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if t is None:
        print(f"Tenant '{slug}' nicht gefunden.")
        return
    provider = await _build_lexware_provider(t.id)
    if provider is None:
        print(f"Tenant '{slug}' hat keinen (aktiven) Lexware-Key.")
        return

    print(f"Betrieb: {t.company_name} ({slug})")
    print(f"API: {LEXWARE_API_BASE}\n")
    print(f"{'Status':<7} {'Treffer':<9} Endpunkt / Bedeutung")
    print("-" * 78)

    async with httpx.AsyncClient(timeout=30.0) as client:
        for pfad, beschreibung in PROBES:
            try:
                r = await client.get(f"{LEXWARE_API_BASE}{pfad}", headers=provider._headers)
                code = r.status_code
                treffer = ""
                if code == 200:
                    try:
                        data = r.json()
                        if isinstance(data, dict) and "totalElements" in data:
                            treffer = str(data["totalElements"])
                        elif isinstance(data, list):
                            treffer = str(len(data))
                        else:
                            treffer = "obj"
                    except ValueError:
                        treffer = "?"
                print(f"{code:<7} {treffer:<9} {pfad.split('?')[0]:<34} {beschreibung}")
            except Exception as exc:  # noqa: BLE001
                print(f"{'ERR':<7} {'':<9} {pfad:<34} {type(exc).__name__}")
            await asyncio.sleep(0.6)  # Lexware limitiert auf 2 Anfragen/Sekunde

        # Profil ausfuehrlich — daraus laesst sich am meisten ableiten.
        print("\n--- /v1/profile im Detail ---")
        r = await client.get(f"{LEXWARE_API_BASE}/v1/profile", headers=provider._headers)
        if r.status_code == 200:
            prof = r.json()
            prof.pop("companyId", None)
            prof.pop("organizationId", None)
            prof.pop("userId", None)
            print(json.dumps(prof, indent=2, ensure_ascii=False))
        else:
            print(f"HTTP {r.status_code}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "pilot"))
