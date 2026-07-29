"""Import-Skript Kundendatenbank Phase 8 — Lexware-Kontakte einmalig
in den Kundenstamm importieren (Onboarding).

Richtung: einmalig Lexware -> Gewerbeagent. Danach fuehrt Gewerbeagent
und schreibt via upsert_customer_contact() (Phase 4) zurueck — kein
kontinuierlicher Zwei-Wege-Sync.

Ablauf pro Kontakt (nur Kunden-Rolle, seitenweise via
list_contacts_page, Rate-Limit steckt im Provider):
- Existiert schon ein kunde_external_ref ("lexware", contact_id):
  ueberspringen — idempotent, wiederholbar. Das deckt zugleich die
  Richtung "Kontakt-ID haengt schon an einem anderen Kunden" ab.
- Kontakt mit Mail oder Telefon: Aufloesung ueber die Lookup-Kaskade
  des Identity-Service — existiert der Kunde schon (z. B. weil er vor
  dem Import eine Anfrage gestellt hat), wird nur der Ref angehaengt
  und fehlende Merkmale ergaenzt, kein zweiter Kunde.
- Nur-Name-Kontakt: resolve_kunde_name_only — RAET NIE. Bei
  Namensgleichheit mit Bestandskunden: needs_review an den Kandidaten
  + Report-Zeile, kein Ref (Aufloesung via scripts/merge_kunden.py,
  danach Import einfach nochmal laufen lassen).
- Hat der aufgeloeste Kunde schon einen ANDEREN Lexware-Ref (zwei
  Lexware-Kontakte mit derselben Mail): kein Ref, needs_review +
  Report-Zeile — nie ueberschreiben.
- Kontakt ohne Name, Mail und Telefon: nur Report-Zeile.

Mapping (verlustbehaftet, die Wahrheit bleibt ueber die external_id
erreichbar): company.name bzw. firstName + lastName -> name; erste
Mail nach Prioritaet business > office > private > other -> email;
Telefon nach derselben Prioritaet (plus mobile) -> telefon;
Billing-Adresse (street, zip, city) -> adresse.

Aufruf (im Container):
  Trockenlauf:   uv run python -m scripts.import_lexware_contacts --tenant sven
  Scharf:        uv run python -m scripts.import_lexware_contacts --tenant sven --execute
  Report-CSV:    ... --report-csv /tmp/import_report.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass, field

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.integrations.lexware import LexwareProvider
from core.integrations.rechnung_payment_monitor import (
    _build_lexware_provider,
)
from core.models import (
    Kunde,
    KundeExternalRef,
    REF_SYSTEM_LEXWARE,
    Tenant,
)
from core.services.kunde_identity import (
    compose_adresse,
    find_kunden_by_name,
    resolve_kunde,
    resolve_kunde_name_only,
    resolve_or_create_kunde,
)

# Seitengroesse fuers Paging — bei 2 req/s und ein paar hundert
# Kontakten dauert der Lauf Minuten, unkritisch weil einmalig.
PAGE_SIZE = 100

_EMAIL_PRIO = ("business", "office", "private", "other")
_TEL_PRIO = ("business", "office", "mobile", "private", "other")


@dataclass
class Stats:
    kontakte_gesamt: int = 0
    schon_importiert: int = 0
    kunden_neu: int = 0
    kunden_verknuepft: int = 0
    refs_angelegt: int = 0
    mehrdeutig: int = 0
    ref_konflikte: int = 0
    ohne_kundendaten: int = 0
    report: list[dict] = field(default_factory=list)

    def melde(self, contact_id: str, grund: str, name: str | None = None,
              details: str = "") -> None:
        self.report.append({
            "contact_id": contact_id, "grund": grund,
            "name": name or "", "details": details,
        })


def _erste(werte_nach_art: dict | None, prioritaet: tuple[str, ...]) -> str | None:
    """Erster Eintrag nach Prioritaetsliste (Muster wie in
    lexware.search_contacts fuer emailAddresses)."""
    for art in prioritaet:
        lst = (werte_nach_art or {}).get(art) or []
        if lst:
            wert = (lst[0] or "").strip() if isinstance(lst[0], str) else lst[0]
            if wert:
                return wert
    return None


def _mappe_kontakt(entry: dict) -> tuple[str | None, str, str | None,
                                         str | None, str | None]:
    """Rohes Lexware-Kontakt-Objekt -> (contact_id, name, email,
    telefon, adresse). contact_id None bei kaputtem Eintrag."""
    cid = str(entry.get("id") or "").strip().lower() or None

    company = entry.get("company") or {}
    person = entry.get("person") or {}
    name = (
        (company.get("name") or "").strip()
        or " ".join(p.strip() for p in [
            person.get("firstName"), person.get("lastName"),
        ] if p and p.strip())
    )

    email = _erste(entry.get("emailAddresses"), _EMAIL_PRIO)
    telefon = _erste(entry.get("phoneNumbers"), _TEL_PRIO)

    adresse = None
    billing = (entry.get("addresses") or {}).get("billing") or []
    if billing:
        b = billing[0] or {}
        adresse = compose_adresse(b.get("street"), b.get("zip"), b.get("city"))

    return cid, name, email, telefon, adresse


async def _importiere_kontakt(s, tenant: Tenant, entry: dict,
                              stats: Stats) -> None:
    cid, name, email, telefon, adresse = _mappe_kontakt(entry)
    if cid is None:
        stats.melde("?", "kaputter-eintrag", details=str(entry)[:200])
        return
    stats.kontakte_gesamt += 1

    # Idempotenz + Fremd-Pinnung in einem: existiert der Ref schon
    # (egal an welchem Kunden), ist dieser Kontakt erledigt.
    ref_da = (await s.execute(
        select(KundeExternalRef).where(
            KundeExternalRef.tenant_id == tenant.id,
            KundeExternalRef.system == REF_SYSTEM_LEXWARE,
            KundeExternalRef.external_id == cid,
        )
    )).scalars().first()
    if ref_da is not None:
        stats.schon_importiert += 1
        return

    if not name and not email and not telefon:
        stats.ohne_kundendaten += 1
        stats.melde(cid, "ohne-kundendaten")
        return

    if email or telefon:
        # Starke Identitaet — Kaskade findet den Bestandskunden oder
        # legt neu an; fehlende Merkmale werden am Treffer ergaenzt.
        vorher = await resolve_kunde(s, tenant.id, name=name,
                                     email=email, telefon=telefon)
        kunde = await resolve_or_create_kunde(
            s, tenant.id, name or "", email=email, telefon=telefon,
            adresse=adresse,
        )
        neu = vorher is None
    else:
        # Nur-Name-Kontakt: nie raten — dieselbe Regel wie Backfill
        # und Phase-6-Rueckfrage.
        vorher_da = bool(await find_kunden_by_name(s, tenant.id, name))
        kunde_id, kandidaten = await resolve_kunde_name_only(
            s, tenant.id, name)
        if kunde_id is None:
            for k in kandidaten:
                k.needs_review = True
            stats.mehrdeutig += 1
            stats.melde(
                cid, "mehrdeutig", name,
                details="Kandidaten: " + ", ".join(
                    f"{k.id} ({k.identity_key})" for k in kandidaten
                ) + " — via merge_kunden.py aufloesen, dann Import "
                "wiederholen",
            )
            return
        neu = not vorher_da
        kunde = await s.get(Kunde, kunde_id)
        if adresse and not kunde.adresse:
            kunde.adresse = adresse

    # Zweiter Lexware-Kontakt fuer denselben Kunden (z. B. dieselbe
    # Mail an zwei Lexware-Kontakten): nie ueberschreiben. Nur fuer
    # Bestandskunden moeglich — ein frisch angelegter hat keinen Ref.
    eigener_ref = (await s.execute(
        select(KundeExternalRef).where(
            KundeExternalRef.tenant_id == tenant.id,
            KundeExternalRef.kunde_id == kunde.id,
            KundeExternalRef.system == REF_SYSTEM_LEXWARE,
        )
    )).scalars().first()
    if eigener_ref is not None:
        kunde.needs_review = True
        stats.ref_konflikte += 1
        stats.melde(
            cid, "zweiter-lexware-kontakt", name,
            details=f"Kunde {kunde.id} hat schon Ref "
                    f"{eigener_ref.external_id} — in Lexware klaeren, "
                    "welcher Kontakt gilt",
        )
        return

    if neu:
        stats.kunden_neu += 1
    else:
        stats.kunden_verknuepft += 1
    s.add(KundeExternalRef(
        tenant_id=tenant.id, kunde_id=kunde.id,
        system=REF_SYSTEM_LEXWARE, external_id=cid,
    ))
    stats.refs_angelegt += 1


async def import_kontakte(
    tenant_slug: str,
    *,
    execute: bool,
    provider: LexwareProvider | None = None,
) -> Stats:
    """Importiert alle Lexware-Kunden-Kontakte eines Tenants.

    `provider` ist fuer Tests injizierbar (FakeLexware-Muster);
    ohne Angabe wird er aus der ToolConfig des Tenants gebaut.
    """
    stats = Stats()
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )).scalars().first()
        if tenant is None:
            print(f"Kein Tenant mit Slug {tenant_slug!r}", file=sys.stderr)
            return stats

        if provider is None:
            provider = await _build_lexware_provider(tenant.id)
        if provider is None:
            print(f"Tenant {tenant_slug!r} hat keine aktive "
                  "Lexware-Konfiguration (ToolConfig).", file=sys.stderr)
            return stats

        page = 0
        while True:
            entries, last = await provider.list_contacts_page(
                page=page, size=PAGE_SIZE)
            for entry in entries:
                await _importiere_kontakt(s, tenant, entry, stats)
            if last or not entries:
                break
            page += 1

        if execute:
            await s.commit()
        else:
            await s.rollback()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kundendatenbank Phase 8: einmaliger Lexware-Import "
                    "beim Onboarding (Trockenlauf ohne --execute)",
    )
    parser.add_argument("--tenant", required=True,
                        help="Tenant-Slug (Import ist pro Tenant)")
    parser.add_argument("--execute", action="store_true",
                        help="Aenderungen wirklich schreiben "
                             "(sonst Trockenlauf)")
    parser.add_argument("--report-csv",
                        help="Report zusaetzlich als CSV schreiben")
    args = parser.parse_args()

    stats = asyncio.run(import_kontakte(args.tenant, execute=args.execute))

    modus = "SCHARF" if args.execute else "TROCKENLAUF (nichts geschrieben)"
    print(f"\n=== Lexware-Import-Ergebnis [{modus}] ===")
    print(f"Kontakte gesehen:    {stats.kontakte_gesamt}")
    print(f"schon importiert:    {stats.schon_importiert}")
    print(f"Kunden neu angelegt: {stats.kunden_neu}")
    print(f"Bestand verknuepft:  {stats.kunden_verknuepft}")
    print(f"Refs angelegt:       {stats.refs_angelegt}")
    print(f"mehrdeutig:          {stats.mehrdeutig}")
    print(f"Ref-Konflikte:       {stats.ref_konflikte}")
    print(f"ohne Kundendaten:    {stats.ohne_kundendaten}")
    for zeile in stats.report:
        print(f"  [{zeile['grund']}] {zeile['contact_id']} "
              f"{zeile['name']} — {zeile['details']}")

    if args.report_csv and stats.report:
        with open(args.report_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(stats.report[0].keys()))
            w.writeheader()
            w.writerows(stats.report)
        print(f"Report-CSV: {args.report_csv} ({len(stats.report)} Zeilen)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
