#!/usr/bin/env python3
"""DSAR — Data Subject Access / Erasure Request (Art. 15 / 17 / 20 DSGVO).

Findet, exportiert und (optional) loescht ALLE personenbezogenen Daten
eines einzelnen Endkunden eines Betriebs — der Baustein, den die
Betroffenenrechte brauchen und der bisher fehlte (`delete_tenant.py`
loescht nur ganze Betriebe).

Identifikation ueber E-Mail und/oder Telefon (exakt) — optional Name
(unscharf, nur wo kein E-Mail/Telefon-Feld existiert, z.B. Transkripte).

WICHTIG — was NICHT geloescht wird (Art. 17 Abs. 3 lit. b DSGVO):
Rechnungen, Angebote, Belege und der Lexware-Kontakt unterliegen der
gesetzlichen Aufbewahrungspflicht (GoBD / § 147 AO / § 257 HGB, i.d.R.
10 Jahre). Diese werden NUR GEMELDET, nicht geloescht — der Betrieb muss
sie manuell nach Fristablauf entfernen.

Aufruf:
  # Auskunft/Export (Art. 15/20) — nichts wird veraendert:
  uv run python -m scripts.dsar --tenant demo --email kunde@example.de

  # Loeschung Vorschau (Dry-Run):
  uv run python -m scripts.dsar --tenant demo --email kunde@x.de --mode delete

  # Loeschung scharf (inkl. Drive-Ordner, inkl. Name-basierter Transkripte):
  uv run python -m scripts.dsar --tenant demo --email kunde@x.de \\
      --phone "+49170..." --name "Max Mustermann" \\
      --mode delete --execute --with-drive --name-match
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys

from sqlalchemy import delete, func, select

from core.database import AsyncSessionLocal
from core.models import (
    AnfrageToken,
    EmailConversation,
    Kundengespraech,
    Tenant,
    TenantKundeDrive,
    Visualisierung,
)
from core.utils.phone import normalize_phone

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("dsar")


# ---------------------------------------------------------------------------
# Matching-Helfer
# ---------------------------------------------------------------------------

def _email_matches(row_email: str | None, email_norm: str | None) -> bool:
    return bool(
        email_norm and row_email and row_email.strip().lower() == email_norm
    )


def _phone_matches(row_phone: str | None, phone_norm: str | None) -> bool:
    return bool(
        phone_norm and row_phone and normalize_phone(row_phone) == phone_norm
    )


# ---------------------------------------------------------------------------
# Sammeln (Art. 15 / 20)
# ---------------------------------------------------------------------------

async def collect(
    *, tenant_id, email_norm: str | None, phone_norm: str | None,
    name: str | None,
) -> dict:
    """Sammelt alle gefundenen personenbezogenen Daten, tenant-scoped.

    Liefert ein serialisierbares dict mit den Treffern pro Quelle plus
    den IDs (fuer eine etwaige Loeschung). Name-basierte Treffer
    (Transkripte) werden separat markiert, weil sie unschaerfer sind.
    """
    name_norm = name.strip().lower() if name else None
    out: dict = {
        "email_conversations": [],
        "anfragen": [],
        "drive_folders": [],
        "kundengespraeche_name_match": [],
        "visualisierungen": [],
        "_ids": {
            "email_conversations": [],
            "anfragen": [],
            "drive": [],
            "kundengespraeche": [],
            "visualisierungen": [],         # praezise (E-Mail-Match)
            "visualisierungen_name": [],    # unscharf (nur Name-Match)
        },
    }

    async with AsyncSessionLocal() as s:
        # --- EmailConversation (nur E-Mail-Feld) ---
        if email_norm:
            rows = (await s.execute(
                select(EmailConversation)
                .where(EmailConversation.tenant_id == tenant_id)
                .where(func.lower(EmailConversation.kunde_email) == email_norm)
            )).scalars().all()
            for r in rows:
                out["email_conversations"].append({
                    "id": str(r.id),
                    "kunde_email": r.kunde_email,
                    "kunde_name": r.kunde_name,
                    "state": r.state,
                    "termin_datum": r.termin_datum,
                    "created_at": getattr(r, "created_at", None),
                })
                out["_ids"]["email_conversations"].append(r.id)

        # --- AnfrageToken (+ Responses via FK-CASCADE) ---
        cand = (await s.execute(
            select(AnfrageToken).where(AnfrageToken.tenant_id == tenant_id)
        )).scalars().all()
        for r in cand:
            if (_email_matches(r.kunde_email, email_norm)
                    or _phone_matches(r.kunde_telefon, phone_norm)):
                out["anfragen"].append({
                    "id": str(r.id),
                    "kunde_email": r.kunde_email,
                    "kunde_name": r.kunde_name,
                    "kunde_telefon": r.kunde_telefon,
                    "anfrage_typ": r.anfrage_typ,
                    "created_at": r.created_at,
                    "submitted_at": r.submitted_at,
                })
                out["_ids"]["anfragen"].append(r.id)

        # --- TenantKundeDrive (Mapping → Drive-Ordner) ---
        dcand = (await s.execute(
            select(TenantKundeDrive)
            .where(TenantKundeDrive.tenant_id == tenant_id)
        )).scalars().all()
        for r in dcand:
            if (_email_matches(r.kunde_email, email_norm)
                    or _phone_matches(r.kunde_telefon, phone_norm)):
                out["drive_folders"].append({
                    "id": str(r.id),
                    "kunde_name": r.kunde_name,
                    "kunde_email": r.kunde_email,
                    "kunde_telefon": r.kunde_telefon,
                    "drive_folder_id": r.drive_folder_id,
                    "drive_folder_url": r.drive_folder_url,
                })
                out["_ids"]["drive"].append(
                    (r.id, r.drive_folder_id)
                )

        # --- Kundengespraeche (nur Name-Match — kein E-Mail/Telefon-Feld) ---
        if name_norm:
            gcand = (await s.execute(
                select(Kundengespraech)
                .where(Kundengespraech.tenant_id == tenant_id)
                .where(func.lower(Kundengespraech.kunde_name) == name_norm)
            )).scalars().all()
            for r in gcand:
                out["kundengespraeche_name_match"].append({
                    "id": str(r.id),
                    "kunde_name": r.kunde_name,
                    "gespraech_datum": r.gespraech_datum,
                    "hat_transkript": bool(r.raw_transcript),
                    "termin_datum": r.termin_datum,
                })
                out["_ids"]["kundengespraeche"].append(r.id)

        # --- Visualisierungen (kunde_email-Match ODER name-Match) ---
        # Enthaelt kunde_email/kunde_name + Original-/Ergebnis-Foto (PII).
        if email_norm or name_norm:
            vcand = (await s.execute(
                select(Visualisierung)
                .where(Visualisierung.tenant_id == tenant_id)
            )).scalars().all()
            for r in vcand:
                match_email = bool(email_norm and _email_matches(r.kunde_email, email_norm))
                match_name = bool(
                    name_norm and (r.kunde_name or "").strip().lower() == name_norm
                )
                if match_email or match_name:
                    out["visualisierungen"].append({
                        "id": str(r.id),
                        "kunde_email": r.kunde_email,
                        "kunde_name": r.kunde_name,
                        "status": r.status,
                        "hat_original_foto": r.original_image_data is not None,
                        "hat_ergebnis_foto": r.result_image_data is not None,
                        "created_at": getattr(r, "created_at", None),
                        "match": "email" if match_email else "name",
                    })
                    # E-Mail-Match ist praezise (sofort loeschbar), reiner
                    # Name-Match ist unscharf -> nur mit --name-match loeschen.
                    if match_email:
                        out["_ids"]["visualisierungen"].append(r.id)
                    else:
                        out["_ids"]["visualisierungen_name"].append(r.id)

    return out


async def lexware_report(*, tenant_id, name: str | None) -> list[dict]:
    """Best-effort Lexware-Kontakt-Suche (NUR Report, keine Loeschung).

    Lexware-Kontakte haengen an Rechnungen → gesetzliche Aufbewahrung.
    Faellt still aus, wenn Lexware fuer den Tenant nicht konfiguriert ist.
    """
    if not name or len(name.strip()) < 3:
        return []
    try:
        from core.integrations.rechnung_payment_monitor import (
            _build_lexware_provider,
        )
        provider = await _build_lexware_provider(tenant_id)
        if provider is None:
            return []
        matches = await provider.search_contacts(name=name)
        return [
            {
                "contact_id": str(getattr(m, "contact_id", "")),
                "name": getattr(m, "name", None),
                "email": getattr(m, "email", None),
            }
            for m in (matches or [])
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Lexware-Report uebersprungen: {e}")
        return []


# ---------------------------------------------------------------------------
# Loeschung (Art. 17)
# ---------------------------------------------------------------------------

async def _delete_drive_folder(tenant_id, folder_id: str) -> bool:
    """Loescht den Drive-Ordner permanent. True bei Erfolg/schon-weg."""
    try:
        from core.integrations.google_drive import get_drive_service
        service = await get_drive_service(tenant_id)
        await asyncio.to_thread(
            lambda: service.files().delete(fileId=folder_id).execute()
        )
        return True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "404" in msg or "notFound" in msg:
            logger.info(f"  Drive-Ordner {folder_id} schon weg (404).")
            return True
        logger.warning(f"  Drive-Loeschung {folder_id} fehlgeschlagen: {e}")
        return False


async def erase(
    *, tenant_id, found: dict, execute: bool, with_drive: bool,
    name_match: bool,
) -> dict:
    """Loescht die loeschbaren Treffer. Dry-Run wenn execute=False."""
    ec_ids = found["_ids"]["email_conversations"]
    af_ids = found["_ids"]["anfragen"]
    drive = found["_ids"]["drive"]
    gespraech_ids = found["_ids"]["kundengespraeche"]
    vis_ids = found["_ids"]["visualisierungen"]
    vis_name_ids = found["_ids"]["visualisierungen_name"]
    # Praezise (E-Mail) immer; reiner Name-Match nur mit --name-match.
    vis_delete_ids = vis_ids + (vis_name_ids if name_match else [])

    stats = {
        "email_conversations": len(ec_ids),
        "anfragen": len(af_ids),
        "drive_db": len(drive),
        "drive_folders_deleted": 0,
        "kundengespraeche": len(gespraech_ids) if name_match else 0,
        "visualisierungen": len(vis_delete_ids),
    }

    if not execute:
        return stats

    async with AsyncSessionLocal() as s:
        if ec_ids:
            await s.execute(
                delete(EmailConversation)
                .where(EmailConversation.id.in_(ec_ids))
            )
        if af_ids:
            # anfrage_responses haengen per FK ON DELETE CASCADE dran.
            await s.execute(
                delete(AnfrageToken).where(AnfrageToken.id.in_(af_ids))
            )
        if name_match and gespraech_ids:
            await s.execute(
                delete(Kundengespraech)
                .where(Kundengespraech.id.in_(gespraech_ids))
            )
        if vis_delete_ids:
            await s.execute(
                delete(Visualisierung)
                .where(Visualisierung.id.in_(vis_delete_ids))
            )
        # Drive: erst den echten Ordner, dann die Mapping-Zeile.
        for row_id, folder_id in drive:
            if with_drive and folder_id:
                ok = await _delete_drive_folder(tenant_id, folder_id)
                if ok:
                    stats["drive_folders_deleted"] += 1
            await s.execute(
                delete(TenantKundeDrive)
                .where(TenantKundeDrive.id == row_id)
            )
        await s.commit()

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _resolve_tenant(slug: str):
    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(Tenant).where(Tenant.slug == slug.lower())
        )).scalar_one_or_none()


async def _main(args) -> int:
    tenant = await _resolve_tenant(args.tenant)
    if tenant is None:
        logger.error(f"Tenant '{args.tenant}' nicht gefunden.")
        return 1

    email_norm = args.email.strip().lower() if args.email else None
    phone_norm = normalize_phone(args.phone) if args.phone else None
    if not (email_norm or phone_norm or args.name):
        logger.error("Mindestens --email, --phone oder --name angeben.")
        return 1

    logger.info(
        f"=== DSAR [{args.mode.upper()}] tenant={tenant.slug} "
        f"email={email_norm or '-'} phone={phone_norm or '-'} "
        f"name={args.name or '-'} ==="
    )

    found = await collect(
        tenant_id=tenant.id, email_norm=email_norm,
        phone_norm=phone_norm, name=args.name,
    )
    lex = await lexware_report(tenant_id=tenant.id, name=args.name)

    n_ec = len(found["email_conversations"])
    n_af = len(found["anfragen"])
    n_dr = len(found["drive_folders"])
    n_ge = len(found["kundengespraeche_name_match"])
    n_vi = len(found["visualisierungen"])
    n_vi_name = len(found["_ids"]["visualisierungen_name"])
    logger.info(
        f"Gefunden: {n_ec} Mail-Konversationen, {n_af} Anfragen, "
        f"{n_dr} Drive-Ordner, {n_ge} Transkripte (Name-Match), "
        f"{n_vi} Visualisierungen, "
        f"{len(lex)} Lexware-Kontakte (nur Report)."
    )

    if args.mode == "export":
        # Interne ID-Helfer aus dem Export entfernen.
        export_obj = {k: v for k, v in found.items() if k != "_ids"}
        export_obj["lexware_kontakte_retained"] = lex
        export_obj["_meta"] = {
            "tenant": tenant.slug,
            "email": email_norm, "phone": phone_norm, "name": args.name,
            "erstellt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "hinweis": (
                "Rechnungen/Angebote/Belege + Lexware-Kontakte unterliegen "
                "der gesetzlichen Aufbewahrung (Art. 17 Abs. 3 DSGVO) und "
                "sind hier nur referenziert, nicht enthalten/loeschbar."
            ),
        }
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        fname = f"dsar_export_{tenant.slug}_{ts}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(export_obj, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"Export geschrieben: {fname}")
        return 0

    # mode == delete
    if n_ge and not args.name_match:
        logger.info(
            f"Hinweis: {n_ge} Transkript(e) per Name gefunden — werden NUR "
            f"mit --name-match geloescht (Name-Match ist unscharf)."
        )
    if n_vi_name and not args.name_match:
        logger.info(
            f"Hinweis: {n_vi_name} Visualisierung(en) NUR per Name (ohne "
            f"E-Mail-Match) gefunden — werden NUR mit --name-match geloescht."
        )
    if n_dr and not args.with_drive:
        logger.info(
            f"Hinweis: {n_dr} Drive-Ordner-Mapping(s) gefunden — der echte "
            f"Drive-Ordner wird nur mit --with-drive geloescht (sonst nur "
            f"die DB-Zuordnung)."
        )

    stats = await erase(
        tenant_id=tenant.id, found=found, execute=args.execute,
        with_drive=args.with_drive, name_match=args.name_match,
    )

    verb = "GELOESCHT" if args.execute else "wuerde loeschen (Dry-Run)"
    logger.info(
        f"{verb}: {stats['email_conversations']} Mail-Konversationen, "
        f"{stats['anfragen']} Anfragen (+Responses), "
        f"{stats['kundengespraeche']} Transkripte, "
        f"{stats['drive_db']} Drive-Mappings "
        f"(davon {stats['drive_folders_deleted']} Ordner real geloescht)."
    )
    if lex:
        logger.info(
            f"NICHT geloescht (gesetzliche Aufbewahrung): "
            f"{len(lex)} Lexware-Kontakt(e) — manuell nach Fristablauf "
            f"pruefen: {[c['contact_id'] for c in lex]}"
        )
    if not args.execute:
        logger.info("Dry-Run — nichts veraendert. Mit --execute scharf laufen.")
    return 0


def cli() -> None:
    p = argparse.ArgumentParser(
        description="DSAR — Auskunft/Loeschung personenbezogener Daten "
                    "eines Endkunden (Art. 15/17/20 DSGVO)."
    )
    p.add_argument("--tenant", required=True, help="Tenant-Slug")
    p.add_argument("--email", help="E-Mail des Betroffenen")
    p.add_argument("--phone", help="Telefon des Betroffenen")
    p.add_argument("--name", help="Name (nur Transkript-/Lexware-Match)")
    p.add_argument(
        "--mode", choices=("export", "delete"), default="export",
        help="export = Auskunft (Default), delete = Loeschung",
    )
    p.add_argument(
        "--execute", action="store_true",
        help="Bei --mode delete WIRKLICH loeschen (sonst Dry-Run).",
    )
    p.add_argument(
        "--with-drive", action="store_true",
        help="Auch den echten Google-Drive-Ordner loeschen.",
    )
    p.add_argument(
        "--name-match", action="store_true",
        help="Auch per Name gematchte Transkripte loeschen (unscharf).",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    cli()
