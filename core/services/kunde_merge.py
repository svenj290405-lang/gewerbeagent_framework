"""Kunden-Merge — Kunde B (Quelle) geht in Kunde A (Ziel) auf.

Kernlogik hinter scripts/merge_kunden.py (CLI) und dem App-Endpoint
POST /app/api/kunden/merge (Zusammenfuehren-Karte im Kundenprofil).
Commit/Rollback liegt beim Aufrufer — der Service veraendert nur die
Session. Regeln siehe Kundendatenbank_Umsetzungsplan.md, Phase 8:
FKs aller acht Bestandstabellen umhaengen, External-Refs verschieben,
fehlende Merkmale uebernehmen, Quelle additive-only per merged_into_id
stilllegen, Ketten einstufig halten.

Abbruch (MergeAbbruch, nichts veraendert): Quelle/Ziel fehlen,
Tenant-Grenze, Zyklus, Quelle schon anderswo gemergt, oder beide haben
einen External-Ref im selben System (nie ueberschreiben — erst im
Fremdsystem klaeren).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import (
    Angebot,
    AnfrageToken,
    EmailConversation,
    Kunde,
    KundeExternalRef,
    Kundengespraech,
    Rechnung,
    Rueckruf,
    TenantKundeDrive,
    Visualisierung,
)
from core.services.kunde_identity import _follow_merge

# Alle Bestandstabellen mit kunde_id-FK (siehe Umsetzungsplan,
# Datenmodell) — dieselben acht wie im Backfill.
FK_TABELLEN = (
    Angebot,
    Rechnung,
    Kundengespraech,
    Rueckruf,
    AnfrageToken,
    EmailConversation,
    Visualisierung,
    TenantKundeDrive,
)


class MergeAbbruch(Exception):
    """Fachlicher Abbruch — Meldung fuer Mensch/UI statt Stack-Trace."""


@dataclass
class MergeErgebnis:
    """Was der Merge getan hat — fuers CLI-Protokoll und die App-Antwort."""
    quelle: Kunde
    ziel: Kunde                       # finales Ziel (nach Kettenaufloesung)
    schon_gemergt: bool = False       # No-op: Quelle war schon in diesem Ziel
    ziel_war_gemergt: bool = False    # Ziel-Kette wurde vorab aufgeloest
    umgehaengt: dict[str, int] = field(default_factory=dict)
    refs_verschoben: list[str] = field(default_factory=list)
    merkmale_uebernommen: list[str] = field(default_factory=list)
    ketten_umgehaengt: int = 0
    drive_keys: list[str] = field(default_factory=list)  # >1 = Hinweis


async def _lade_kunde(
    s: AsyncSession, kunde_id: uuid.UUID, rolle: str,
) -> Kunde:
    kunde = await s.get(Kunde, kunde_id, with_for_update=True)
    if kunde is None:
        raise MergeAbbruch(f"{rolle} {kunde_id} existiert nicht.")
    return kunde


async def merge_kunden(
    s: AsyncSession,
    quelle_id: uuid.UUID,
    ziel_id: uuid.UUID,
    *,
    clear_review: bool = False,
    feld_wahl: dict[str, str] | None = None,
) -> MergeErgebnis:
    """Fuehrt die Quelle ins Ziel ueber. Wirft MergeAbbruch, bevor
    irgendetwas veraendert wurde; committet nie selbst.

    `feld_wahl` (App-Dialog „Hauptdaten waehlen"): pro Feld (name,
    email, telefon, adresse) entweder "ziel" (Default — Ziel-Wert bleibt,
    Luecken werden aus der Quelle gefuellt) oder "quelle" (Quell-Wert
    ersetzt den Ziel-Wert; der alte Ziel-Wert ist danach an keiner
    aktiven Zeile mehr — die einzige Stelle, an der der Merge bewusst
    ueberschreibt, weil der Mensch es explizit so gewaehlt hat). Eine
    "quelle"-Wahl ohne Wert an der Quelle loescht nie den Ziel-Wert."""
    if quelle_id == ziel_id:
        raise MergeAbbruch("Quelle und Ziel sind derselbe Kunde.")

    quelle = await _lade_kunde(s, quelle_id, "Quelle")
    ziel = await _lade_kunde(s, ziel_id, "Ziel")

    if quelle.tenant_id != ziel.tenant_id:
        raise MergeAbbruch(
            f"Tenant-Grenze: Quelle gehoert zu {quelle.tenant_id}, "
            f"Ziel zu {ziel.tenant_id} — Kunden verschiedener Tenants "
            "werden nie gemergt."
        )

    # Kette des Ziels vorab aufloesen, damit merged_into_id direkt aufs
    # finale Ziel zeigt und Ketten einstufig bleiben.
    final = await _follow_merge(s, ziel)
    ziel_war_gemergt = final.id != ziel.id
    if ziel_war_gemergt:
        final = await _lade_kunde(s, final.id, "Merge-Ziel")
    if final.id == quelle.id:
        raise MergeAbbruch(
            "Das Ziel ist (ueber seine Merge-Kette) bereits in die "
            "Quelle gemergt — dieser Aufruf wuerde einen Zyklus bauen. "
            "Richtung pruefen."
        )

    ergebnis = MergeErgebnis(
        quelle=quelle, ziel=final, ziel_war_gemergt=ziel_war_gemergt,
    )

    if quelle.merged_into_id is not None:
        if quelle.merged_into_id == final.id:
            ergebnis.schon_gemergt = True
            return ergebnis
        raise MergeAbbruch(
            f"Quelle ist schon in {quelle.merged_into_id} gemergt "
            "(anderes Ziel). Falls das dortige Merge-Ziel gemeint ist, "
            "dieses als Quelle angeben."
        )

    # Ref-Konflikt VOR jeder Aenderung pruefen — nie ueberschreiben.
    quelle_refs = (await s.execute(
        select(KundeExternalRef)
        .where(KundeExternalRef.kunde_id == quelle.id)
        .with_for_update()
    )).scalars().all()
    ziel_systeme = {
        r.system: r for r in (await s.execute(
            select(KundeExternalRef)
            .where(KundeExternalRef.kunde_id == final.id)
        )).scalars().all()
    }
    konflikte = [r for r in quelle_refs if r.system in ziel_systeme]
    if konflikte:
        zeilen = "; ".join(
            f"{r.system}: Quelle -> {r.external_id}, "
            f"Ziel -> {ziel_systeme[r.system].external_id}"
            for r in konflikte
        )
        raise MergeAbbruch(
            "Beide Kunden haben einen External-Ref im selben System "
            f"({zeilen}). Welcher Fremdkontakt gilt, muss erst im "
            "Fremdsystem geklaert werden (z. B. Kontakte in Lexware "
            "zusammenfuehren) — dann den ueberzaehligen Ref entfernen "
            "und neu mergen."
        )

    # FKs der acht Bestandstabellen umhaengen.
    for model in FK_TABELLEN:
        res = await s.execute(
            update(model)
            .where(model.kunde_id == quelle.id)
            .values(kunde_id=final.id)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount:
            ergebnis.umgehaengt[model.__tablename__] = res.rowcount

    # Refs verschieben (Konfliktfreiheit oben sichergestellt).
    for r in quelle_refs:
        r.kunde_id = final.id
        ergebnis.refs_verschoben.append(f"{r.system}={r.external_id}")

    # Merkmale uebernehmen. Default: die Wahrheit am Ziel gewinnt,
    # Quell-Werte fuellen nur Luecken (gleiche Regel wie im
    # Identity-Service). Mit expliziter "quelle"-Wahl (App-Dialog)
    # ersetzt der Quell-Wert den Ziel-Wert. `name` laeuft mit, damit
    # beim Merge ueber Namensgrenzen (Heirat) der neue Name gewaehlt
    # werden kann — der identity_key bleibt wie immer unveraendert.
    for feld in ("name", "email", "telefon", "adresse"):
        wert = getattr(quelle, feld)
        if not wert:
            continue
        nimm_quelle = (feld_wahl or {}).get(feld) == "quelle"
        if nimm_quelle or not getattr(final, feld):
            if getattr(final, feld) != wert:
                setattr(final, feld, wert)
                ergebnis.merkmale_uebernommen.append(feld)

    # Kunden, die schon auf die Quelle zeigen, mit umhaengen — so
    # bleiben Ketten einstufig.
    res = await s.execute(
        update(Kunde)
        .where(Kunde.merged_into_id == quelle.id)
        .values(merged_into_id=final.id)
        .execution_options(synchronize_session=False)
    )
    ergebnis.ketten_umgehaengt = res.rowcount or 0

    quelle.merged_into_id = final.id
    # Die Quelle faellt aus allen Lookups raus — ihr Review-Flag ist
    # damit erledigt.
    quelle.needs_review = False
    if clear_review:
        final.needs_review = False

    # Doppelte Drive-Ordner nur melden — der kunde_key bleibt eindeutig,
    # beide Ordner haengen jetzt am Ziel.
    drive_rows = (await s.execute(
        select(TenantKundeDrive)
        .where(TenantKundeDrive.kunde_id == final.id)
    )).scalars().all()
    ergebnis.drive_keys = [d.kunde_key for d in drive_rows]

    return ergebnis


__all__ = ["merge_kunden", "MergeAbbruch", "MergeErgebnis", "FK_TABELLEN"]
