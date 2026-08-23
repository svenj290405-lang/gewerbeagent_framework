"""Kunden-Identitaet — die eine schmale Stelle fuer Lookup + Anlage.

Alle Pfade, die einen Kunden aufloesen oder anlegen (Schreibpfade
Phase 3, Backfill Phase 2, Lexware-Import Phase 8), laufen hier durch,
damit die Identitaets-Logik nicht ueber die Codebasis gestreut wird.
Hintergrund und Phasen: Kundendatenbank_Umsetzungsplan.md.

Der `identity_key` (Mail > Tel > Namens-Slug, identisch zum
Drive-Ordner-Key) ist nur der Anlage-Schluessel. Der Lookup laeuft
ueber eine Kaskade: exakter identity_key, dann email-Spalte, dann
telefon-Spalte. Sonst wuerde ein Kunde, der erst anruft (tel-Key) und
spaeter mailt, beim Mail-Lookup nicht gefunden und doppelt angelegt.
Trifft eine spaetere Stufe, wird das fehlende Merkmal am Kunden
ergaenzt; der identity_key bleibt unveraendert.

Gemergte Kunden (merged_into_id gesetzt) werden nie zurueckgegeben —
die Aufloesung folgt der Referenz bis zum Merge-Ziel. Ueberspringen
statt folgen waere falsch: der gemergte Kunde behaelt seinen
identity_key, ein Neuanlage-Versuch liefe in den Unique-Constraint.

`_kunde_identity_key`/`_slugify_kunde` lebten historisch in
core/integrations/google_drive.py und werden dort re-importiert —
Drive-Ordner-Keys und Kunden-Keys bleiben dasselbe Format.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.models.kunde import Kunde
from core.utils.phone import normalize_phone, phone_match_key

logger = logging.getLogger(__name__)

# Schutz gegen zyklische merged_into_id-Ketten (sollte durch das
# Merge-Skript nie entstehen — Ketten werden dort einstufig gehalten).
_MAX_MERGE_HOPS = 10


def _slugify_kunde(name: str) -> str:
    """'Müller-Bauunternehmen GmbH' -> 'mueller-bauunternehmen-gmbh'."""
    s = (name or "").strip().lower()
    s = (s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
         .replace("ß", "ss"))
    out = []
    last_dash = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    result = "".join(out).strip("-")
    return result[:120] or "kunde"


def _kunde_identity_key(
    kunde_name: str,
    kunde_email: str | None = None,
    kunde_telefon: str | None = None,
) -> str:
    """Stabile Kunden-Identitaet: E-Mail > Telefon (normalisiert) >
    Namens-Slug (Fallback).

    So teilen sich zwei gleichnamige Kunden NICHT denselben Schluessel,
    und dieselbe Person (gleiche Mail/Telefon) trifft ihren Eintrag auch
    bei leicht abweichendem Namen wieder.
    """
    email = (kunde_email or "").strip().lower()
    if email:
        return f"email:{email}"[:120]
    if kunde_telefon:
        tel = normalize_phone(kunde_telefon)
        if tel:
            return f"tel:{tel}"[:120]
    return _slugify_kunde(kunde_name)[:120]


def compose_adresse(
    strasse: str | None, plz: str | None, ort: str | None,
) -> str | None:
    """'Weg 1', '12345', 'Berlin' -> 'Weg 1, 12345 Berlin' (Teile
    optional). Fuer kunden.adresse aus den Einzelfeldern der
    Bestandstabellen."""
    teile = [t.strip() for t in (strasse,) if t and t.strip()]
    ort_zeile = " ".join(t.strip() for t in (plz, ort) if t and t.strip())
    if ort_zeile:
        teile.append(ort_zeile)
    return ", ".join(teile) or None


def _norm_email(email: str | None) -> str | None:
    e = (email or "").strip().lower()
    return e or None


def _norm_tel(telefon: str | None) -> str | None:
    return normalize_phone(telefon) or None


async def _follow_merge(session: AsyncSession, kunde: Kunde) -> Kunde:
    """Folgt merged_into_id bis zum Merge-Ziel (mit Zyklus-Schutz)."""
    seen: set[uuid.UUID] = set()
    while kunde.merged_into_id is not None:
        if kunde.id in seen or len(seen) >= _MAX_MERGE_HOPS:
            logger.warning(
                "merged_into_id-Kette zyklisch/zu lang ab Kunde %s — "
                "nehme letzten erreichten", kunde.id,
            )
            break
        seen.add(kunde.id)
        target = await session.get(Kunde, kunde.merged_into_id)
        if target is None:
            break
        kunde = target
    return kunde


async def resolve_kunde(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str | None = None,
    email: str | None = None,
    telefon: str | None = None,
    *,
    for_update: bool = False,
) -> Kunde | None:
    """Lookup-Kaskade ohne Anlage: identity_key > email > telefon.

    Liefert None, wenn keine Stufe trifft. Gemergte Kunden werden zum
    Merge-Ziel aufgeloest. `for_update` sperrt die Trefferzeile
    (SELECT FOR UPDATE) fuer race-sichere Schreibpfade.
    """
    email_n = _norm_email(email)
    tel_n = _norm_tel(telefon)
    key = _kunde_identity_key(name or "", email_n, tel_n)

    async def _one(stmt):
        if for_update:
            stmt = stmt.with_for_update()
        return (await session.execute(stmt)).scalars().first()

    kunde = await _one(
        select(Kunde)
        .where(Kunde.tenant_id == tenant_id, Kunde.identity_key == key)
    )
    if kunde is None and email_n:
        kunde = await _one(
            select(Kunde)
            .where(Kunde.tenant_id == tenant_id, Kunde.email == email_n)
            .order_by(Kunde.created_at)
        )
    if kunde is None and tel_n:
        # Suffix-Match (letzte 8 Ziffern) statt exaktem Vergleich:
        # normalize_phone ergaenzt bewusst keine Laendervorwahl, also
        # muessen "+49 170 123..." und "0170/123..." trotzdem dieselbe
        # Person treffen — gleiche Logik wie phone_match_key im
        # kalender-Plugin. Kundenstamm pro Tenant ist klein genug fuer
        # den LIKE-Scan.
        suffix = phone_match_key(tel_n)
        kunde = await _one(
            select(Kunde)
            .where(
                Kunde.tenant_id == tenant_id,
                Kunde.telefon.isnot(None),
                Kunde.telefon.like(f"%{suffix}"),
            )
            .order_by(Kunde.created_at)
        )
    if kunde is None:
        return None
    return await _follow_merge(session, kunde)


async def find_kunden_by_name(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
) -> list[Kunde]:
    """Alle nicht-gemergten Kunden mit exakt diesem Namen
    (case-insensitiv, getrimmt). Fuer Mehrdeutigkeits-Pruefungen bei
    Zeilen ohne Mail/Telefon — dort darf nie geraten werden."""
    n = (name or "").strip().lower()
    if not n:
        return []
    rows = (await session.execute(
        select(Kunde).where(
            Kunde.tenant_id == tenant_id,
            func.lower(func.trim(Kunde.name)) == n,
            Kunde.merged_into_id.is_(None),
        )
    )).scalars().all()
    return list(rows)


def _ergaenze_merkmale(
    kunde: Kunde,
    email_n: str | None,
    tel_n: str | None,
    adresse: str | None,
) -> None:
    # Nur fehlende Merkmale ergaenzen, nie ueberschreiben — die Wahrheit
    # am Kunden gewinnt gegen die einzelne Quellzeile.
    if email_n and not kunde.email:
        kunde.email = email_n
    if tel_n and not kunde.telefon:
        kunde.telefon = tel_n
    if adresse and not kunde.adresse:
        kunde.adresse = adresse


async def resolve_or_create_kunde(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    email: str | None = None,
    telefon: str | None = None,
    *,
    adresse: str | None = None,
) -> Kunde:
    """Kunden aufloesen oder anlegen — race-safe.

    Kaskade wie resolve_kunde (mit Zeilen-Lock); bei Treffer werden
    fehlende Merkmale ergaenzt. Neu angelegt wird nur, wenn alle Stufen
    leer ausgehen. Ein paralleler Insert desselben identity_key wird
    ueber ein SAVEPOINT + Re-Lookup aufgefangen, damit die Transaktion
    des Aufrufers intakt bleibt.

    Vorsicht: entscheidet NICHT ueber Namens-Mehrdeutigkeit — ein
    namensgleicher Kunde mit anderer Identitaet fuehrt zur Neuanlage.
    Pfade, die stattdessen nachfragen/markieren muessen (Backfill,
    Phase-6-Rueckfrage), pruefen vorher via find_kunden_by_name.
    """
    email_n = _norm_email(email)
    tel_n = _norm_tel(telefon)
    display_name = (name or "").strip() or email_n or tel_n or "Unbekannt"

    kunde = await resolve_kunde(
        session, tenant_id, name=display_name, email=email_n,
        telefon=tel_n, for_update=True,
    )
    if kunde is not None:
        _ergaenze_merkmale(kunde, email_n, tel_n, adresse)
        return kunde

    key = _kunde_identity_key(display_name, email_n, tel_n)
    kunde = Kunde(
        tenant_id=tenant_id,
        name=display_name,
        email=email_n,
        telefon=tel_n,
        adresse=adresse,
        identity_key=key,
    )
    try:
        async with session.begin_nested():
            session.add(kunde)
            await session.flush()
        return kunde
    except IntegrityError:
        # Race: jemand hat denselben identity_key gerade angelegt —
        # SAVEPOINT ist zurueckgerollt, Kaskade nochmal (jetzt trifft sie).
        found = await resolve_kunde(
            session, tenant_id, name=display_name, email=email_n,
            telefon=tel_n, for_update=True,
        )
        if found is None:
            raise
        _ergaenze_merkmale(found, email_n, tel_n, adresse)
        return found


async def resolve_kunde_name_only(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
) -> tuple[uuid.UUID | None, list[Kunde]]:
    """Vorsichtige Aufloesung fuer Zeilen, die NUR einen Namen haben
    (Phase 6 / Backfill) — raet nie.

    Liefert (kunde_id, kandidaten):
    - Eindeutig (genau ein Kunde mit diesem Namen, selbst nur-Name-
      Identitaet/Slug-Key): dessen id, kandidaten leer.
    - Niemand mit dem Namen: neuer Slug-Kunde wird angelegt.
    - Sonst mehrdeutig: (None, kandidaten) — der Aufrufer fragt den
      Menschen (Rueckfrage-Buttons) oder markiert needs_review.
    """
    name = (name or "").strip()
    if not name:
        return None, []
    slug = _kunde_identity_key(name)
    kandidaten = {k.id: k for k in await find_kunden_by_name(
        session, tenant_id, name)}
    slug_hit = await resolve_kunde(session, tenant_id, name=name)
    if slug_hit is not None:
        kandidaten[slug_hit.id] = slug_hit

    if not kandidaten:
        kunde = await resolve_or_create_kunde(session, tenant_id, name)
        return kunde.id, []
    if len(kandidaten) == 1:
        einziger = next(iter(kandidaten.values()))
        if einziger.identity_key == slug:
            return einziger.id, []
    return None, list(kandidaten.values())


async def create_kunde_explizit_neu(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
) -> Kunde:
    """Legt bewusst einen NEUEN Kunden an, obwohl der Name schon
    existiert („Nein, das ist ein anderer Thomas Mueller").

    Der Slug-Key ist dann meist schon vergeben — der identity_key
    bekommt ein Zufalls-Suffix. Das ist okay: er ist Anlage-Schluessel,
    kein Lookup-Schluessel; kuenftige Nur-Name-Zeilen zu diesem Namen
    laufen ohnehin in die Mehrdeutigkeits-Rueckfrage.
    """
    name = (name or "").strip() or "Unbekannt"
    slug = _kunde_identity_key(name)
    taken = (await session.execute(
        select(Kunde).where(
            Kunde.tenant_id == tenant_id, Kunde.identity_key == slug,
        )
    )).scalars().first()
    key = slug if taken is None else f"{slug[:110]}~{uuid.uuid4().hex[:8]}"
    kunde = Kunde(tenant_id=tenant_id, name=name, identity_key=key)
    session.add(kunde)
    await session.flush()
    return kunde


def kunde_anzeige_merkmal(kunde: Kunde) -> str:
    """Kleinstes unterscheidendes Merkmal fuer die Anzeige bei
    namensgleichen Kunden: Mail > letzte 4 Tel-Ziffern >
    Erstkontakt-Datum. Normal (eindeutiger Name) zeigt die UI nur den
    Namen — dieses Merkmal kommt erst bei Mehrdeutigkeit dazu.
    """
    if kunde.email:
        return kunde.email
    if kunde.telefon:
        return f"…{kunde.telefon[-4:]}"
    if kunde.created_at is not None:
        return f"seit {kunde.created_at.strftime('%d.%m.%Y')}"
    return "neu"


async def resolve_kunde_id_safe(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str | None,
    email: str | None = None,
    telefon: str | None = None,
    *,
    adresse: str | None = None,
) -> uuid.UUID | None:
    """Failsafe-Variante fuer Erstellstellen (Phase 3): liefert die
    kunde_id oder None, wirft nie.

    Eine Angebots-/Rueckruf-/Gespraechs-Anlage darf niemals an der
    Kundenaufloesung scheitern (Failsafe-Regel) — schlimmstenfalls
    bleibt kunde_id NULL und der naechste Backfill-Lauf holt es nach.
    Zeilen ganz ohne Kundendaten liefern None ohne Log.
    """
    if not ((name or "").strip() or (email or "").strip()
            or (telefon or "").strip()):
        return None
    try:
        kunde = await resolve_or_create_kunde(
            session, tenant_id, name or "", email=email, telefon=telefon,
            adresse=adresse,
        )
        return kunde.id
    except Exception:
        logger.exception(
            "Kundenaufloesung fehlgeschlagen (tenant=%s) — "
            "kunde_id bleibt NULL", tenant_id,
        )
        return None


__all__ = [
    "_kunde_identity_key",
    "_slugify_kunde",
    "resolve_kunde",
    "resolve_or_create_kunde",
    "resolve_kunde_id_safe",
    "resolve_kunde_name_only",
    "create_kunde_explizit_neu",
    "kunde_anzeige_merkmal",
    "find_kunden_by_name",
    "compose_adresse",
]
