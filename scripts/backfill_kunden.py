"""Backfill Kundendatenbank Phase 2 — kunde_id auf Bestandszeilen setzen.

Geht pro Tenant ueber alle kundenbezogenen Tabellen, loest jede Zeile
ueber die Lookup-Kaskade des Identity-Service auf (identity_key >
email > telefon) und setzt kunde_id. Fehlt der Kunde, wird er angelegt.
Reihenfolge: Tabellen mit den reichsten Kontaktdaten zuerst, damit
namens-only Zeilen (Kundengespraeche) am Ende gegen einen moeglichst
vollstaendigen Kundenstamm laufen.

Das Skript RAET NIE:
- Zeile hat nur einen Namen und es existiert schon irgendein Kunde mit
  diesem Namen (egal welcher Identitaet): needs_review an den
  Kandidaten + Report-Zeile, kunde_id bleibt NULL. Zwei Personen
  koennen denselben Namen tragen — das entscheidet ein Mensch
  (Aufloesung spaeter via Merge-Skript, Phase 8).
- Zeile ganz ohne Kundendaten: nur Zaehler im Report, es gibt keinen
  Kunden, den man markieren koennte.

Lexware-Konsolidierung: rechnungen.lexware_contact_id wird nach
kunde_external_ref gezogen (external_id = str(uuid), lowercase mit
Bindestrichen). Konflikte — derselbe Lexware-Kontakt an zwei Kunden,
oder ein Kunde mit zwei Kontakt-IDs — verletzen die Unique-Constraints
und sind genau die Vermischung, die wir beseitigen: kein Ref,
needs_review an allen Beteiligten, Report-Zeile.

Idempotent: Zeilen mit gesetzter kunde_id und vorhandene Refs werden
uebersprungen; needs_review wird nur gesetzt, nie zurueckgenommen.

Aufruf (im Container):
  Trockenlauf:   uv run python -m scripts.backfill_kunden
  Scharf:        uv run python -m scripts.backfill_kunden --execute
  Ein Tenant:    uv run python -m scripts.backfill_kunden --tenant sven
  Report-CSV:    uv run python -m scripts.backfill_kunden --report-csv /tmp/report.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import (
    Angebot,
    AnfrageToken,
    EmailConversation,
    Kunde,
    KundeExternalRef,
    Kundengespraech,
    REF_SYSTEM_LEXWARE,
    Rechnung,
    Rueckruf,
    Tenant,
    TenantKundeDrive,
    Visualisierung,
)
from core.services.kunde_identity import (
    compose_adresse,
    find_kunden_by_name,
    resolve_kunde,
    resolve_kunde_name_only,
    resolve_or_create_kunde,
)


# (Model, name-Attr, email-Attr, telefon-Attr, adresse-Builder) — in
# Backfill-Reihenfolge: reichste Kontaktdaten zuerst, nur-Name zuletzt.
TABELLEN = (
    (AnfrageToken, "kunde_name", "kunde_email", "kunde_telefon", None),
    (EmailConversation, "kunde_name", "kunde_email", None, None),
    (Rueckruf, "kunde_name", "kunde_email", "kunde_telefon", None),
    (TenantKundeDrive, "kunde_name", "kunde_email", "kunde_telefon", None),
    (Rechnung, "kunde_name", "kunde_email", None,
     lambda r: compose_adresse(r.kunde_strasse, r.kunde_plz, r.kunde_ort)),
    (Angebot, "kunde_name", "kunde_email", None,
     lambda r: compose_adresse(r.kunde_strasse, r.kunde_plz, r.kunde_ort)),
    (Visualisierung, "kunde_name", "kunde_email", None, None),
    (Kundengespraech, "kunde_name", None, None, None),
)


@dataclass
class Stats:
    zugeordnet: int = 0
    neu_angelegt: int = 0
    mehrdeutig: int = 0
    ohne_kundendaten: int = 0
    schon_gesetzt: int = 0
    refs_angelegt: int = 0
    ref_konflikte: int = 0
    report: list[dict] = field(default_factory=list)

    def melde(self, tenant_slug: str, tabelle: str, row_id, grund: str,
              name: str | None = None, details: str = "") -> None:
        self.report.append({
            "tenant": tenant_slug, "tabelle": tabelle, "row_id": str(row_id),
            "grund": grund, "name": name or "", "details": details,
        })


async def _backfill_tabelle(s, tenant: Tenant, model, name_attr, email_attr,
                            tel_attr, adresse_fn, stats: Stats) -> None:
    tabelle = model.__tablename__
    rows = (await s.execute(
        select(model).where(
            model.tenant_id == tenant.id, model.kunde_id.is_(None),
        )
    )).scalars().all()
    schon = (await s.execute(
        select(model).where(
            model.tenant_id == tenant.id, model.kunde_id.isnot(None),
        )
    )).scalars().all()
    stats.schon_gesetzt += len(schon)

    for row in rows:
        name = (getattr(row, name_attr, None) or "").strip() or None
        email = (getattr(row, email_attr, None) or "").strip() or None if email_attr else None
        tel = (getattr(row, tel_attr, None) or "").strip() or None if tel_attr else None
        adresse = adresse_fn(row) if adresse_fn else None

        if not name and not email and not tel:
            stats.ohne_kundendaten += 1
            continue

        if email or tel:
            # Starke Identitaet — Kaskade findet oder legt an.
            vorher = await resolve_kunde(s, tenant.id, name=name,
                                         email=email, telefon=tel)
            kunde = await resolve_or_create_kunde(
                s, tenant.id, name or "", email=email, telefon=tel,
                adresse=adresse,
            )
            if vorher is None:
                stats.neu_angelegt += 1
            row.kunde_id = kunde.id
            stats.zugeordnet += 1
            continue

        # Nur-Name-Zeile: niemals raten — gemeinsame Logik mit den
        # Erstellpfaden (Phase 6). Backfill markiert Mehrdeutigkeit
        # als needs_review statt zu fragen.
        vorher_da = bool(await find_kunden_by_name(s, tenant.id, name))
        kunde_id, kandidaten = await resolve_kunde_name_only(
            s, tenant.id, name)
        if kunde_id is not None:
            if not vorher_da:
                stats.neu_angelegt += 1
            row.kunde_id = kunde_id
            stats.zugeordnet += 1
        else:
            for k in kandidaten:
                k.needs_review = True
            stats.mehrdeutig += 1
            stats.melde(
                tenant.slug, tabelle, row.id, "mehrdeutig", name,
                details="Kandidaten: " + ", ".join(
                    f"{k.id} ({k.identity_key})" for k in kandidaten
                ),
            )


async def _konsolidiere_lexware_refs(s, tenant: Tenant, stats: Stats) -> None:
    """rechnungen.lexware_contact_id -> kunde_external_ref, konfliktfest."""
    rows = (await s.execute(
        select(Rechnung).where(
            Rechnung.tenant_id == tenant.id,
            Rechnung.lexware_contact_id.isnot(None),
        )
    )).scalars().all()

    von_kunde: dict[uuid.UUID, set[str]] = {}
    von_contact: dict[str, set[uuid.UUID]] = {}
    for r in rows:
        cid = str(r.lexware_contact_id)
        if r.kunde_id is None:
            stats.melde(tenant.slug, "rechnungen", r.id,
                        "lexware-ref-ohne-kunde", r.kunde_name,
                        details=f"contact_id={cid}")
            continue
        von_kunde.setdefault(r.kunde_id, set()).add(cid)
        von_contact.setdefault(cid, set()).add(r.kunde_id)

    konflikt_kunden: set[uuid.UUID] = set()
    for kunde_id, cids in von_kunde.items():
        if len(cids) > 1:
            konflikt_kunden.add(kunde_id)
            stats.ref_konflikte += 1
            stats.melde(tenant.slug, "kunden", kunde_id,
                        "mehrere-lexware-kontakte",
                        details="contact_ids: " + ", ".join(sorted(cids)))
    for cid, kunden_ids in von_contact.items():
        if len(kunden_ids) > 1:
            konflikt_kunden.update(kunden_ids)
            stats.ref_konflikte += 1
            stats.melde(tenant.slug, "kunde_external_ref", cid,
                        "lexware-kontakt-an-mehreren-kunden",
                        details="kunden: " + ", ".join(str(k) for k in sorted(kunden_ids)))

    for kunde_id in konflikt_kunden:
        kunde = await s.get(Kunde, kunde_id)
        if kunde is not None:
            kunde.needs_review = True

    for kunde_id, cids in von_kunde.items():
        if kunde_id in konflikt_kunden:
            continue
        cid = next(iter(cids))
        vorhandene = (await s.execute(
            select(KundeExternalRef).where(
                KundeExternalRef.tenant_id == tenant.id,
                KundeExternalRef.system == REF_SYSTEM_LEXWARE,
                (KundeExternalRef.kunde_id == kunde_id)
                | (KundeExternalRef.external_id == cid),
            )
        )).scalars().all()
        passt = [v for v in vorhandene
                 if v.kunde_id == kunde_id and v.external_id == cid]
        fremd = [v for v in vorhandene
                 if v.kunde_id != kunde_id or v.external_id != cid]
        if passt:
            continue  # idempotent: Ref existiert schon
        if fremd:
            # Nie ueberschreiben — bestehender Ref widerspricht.
            stats.ref_konflikte += 1
            kunde = await s.get(Kunde, kunde_id)
            if kunde is not None:
                kunde.needs_review = True
            stats.melde(tenant.slug, "kunde_external_ref", kunde_id,
                        "ref-widerspricht-bestand",
                        details=f"neu: {cid}, bestand: "
                        + ", ".join(f"{v.kunde_id}->{v.external_id}" for v in fremd))
            continue
        s.add(KundeExternalRef(
            tenant_id=tenant.id, kunde_id=kunde_id,
            system=REF_SYSTEM_LEXWARE, external_id=cid,
        ))
        stats.refs_angelegt += 1


async def backfill(tenant_slug: str | None, execute: bool) -> Stats:
    stats = Stats()
    async with AsyncSessionLocal() as s:
        stmt = select(Tenant).order_by(Tenant.slug)
        if tenant_slug:
            stmt = stmt.where(Tenant.slug == tenant_slug)
        tenants = (await s.execute(stmt)).scalars().all()
        if not tenants:
            print(f"Kein Tenant gefunden ({tenant_slug=!r})", file=sys.stderr)
            return stats

        for tenant in tenants:
            for model, name_a, email_a, tel_a, adr_fn in TABELLEN:
                await _backfill_tabelle(
                    s, tenant, model, name_a, email_a, tel_a, adr_fn, stats,
                )
            await _konsolidiere_lexware_refs(s, tenant, stats)

        if execute:
            await s.commit()
        else:
            await s.rollback()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kundendatenbank Phase 2: kunde_id-Backfill "
                    "(Trockenlauf ohne --execute)",
    )
    parser.add_argument("--execute", action="store_true",
                        help="Aenderungen wirklich schreiben (sonst Trockenlauf)")
    parser.add_argument("--tenant", help="nur dieser Tenant-Slug")
    parser.add_argument("--report-csv", help="Report zusaetzlich als CSV schreiben")
    args = parser.parse_args()

    stats = asyncio.run(backfill(args.tenant, args.execute))

    modus = "SCHARF" if args.execute else "TROCKENLAUF (nichts geschrieben)"
    print(f"\n=== Backfill-Ergebnis [{modus}] ===")
    print(f"zugeordnet:        {stats.zugeordnet}")
    print(f"davon neu angelegt:{stats.neu_angelegt:>5}")
    print(f"schon gesetzt:     {stats.schon_gesetzt}")
    print(f"mehrdeutig:        {stats.mehrdeutig}")
    print(f"ohne Kundendaten:  {stats.ohne_kundendaten}")
    print(f"Lexware-Refs neu:  {stats.refs_angelegt}")
    print(f"Ref-Konflikte:     {stats.ref_konflikte}")
    for zeile in stats.report:
        print(f"  [{zeile['grund']}] {zeile['tenant']}/{zeile['tabelle']} "
              f"{zeile['row_id']} {zeile['name']} — {zeile['details']}")

    if args.report_csv and stats.report:
        with open(args.report_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(stats.report[0].keys()))
            w.writeheader()
            w.writerows(stats.report)
        print(f"Report-CSV: {args.report_csv} ({len(stats.report)} Zeilen)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
