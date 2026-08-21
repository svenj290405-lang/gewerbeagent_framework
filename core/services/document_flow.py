"""Beleg-Fluss des Kundenzyklus als wiederverwendbarer Service.

Hier liegt die EINE Implementierung von Angebot/Rechnung anlegen + versenden
und Anfrage-Antwort. Genutzt von:
  * den PWA-App-Routen (core/api/app_screens.py),
  * dem Gemini-Assistenten (core/ai/command_center.py),
  * dem Rechnungsversand (_run_rechnung_versand_pipeline).

So existiert die geldkritische Lexware-/Mail-Logik nur an einer Stelle und
kann nicht zwischen den Oberflaechen auseinanderdriften.

Alle Funktionen sind tenant-gescoped (erster Parameter ``tid``) und geben
ein jsonable dict zurueck (immer mit ``ok``-Flag). Sie werfen NICHT bei
fachlichen Fehlern (Lexware fehlt, keine Mail …) — das steht im dict —,
nur unerwartete Bugs propagieren.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _lexware_provider(tid: uuid.UUID):
    """Baut den Lexware-Provider fuer den Tenant (oder None wenn nicht
    eingerichtet). Nutzt die core-Factory aus dem Payment-Monitor."""
    from core.integrations.rechnung_payment_monitor import _build_lexware_provider
    return await _build_lexware_provider(tid)


def _positionen_to_line_items(positionen: list[dict]):
    """Wandelt App-/Extraktions-Positionen in Lexware-LineItems + Summe.

    Position-Felder: name, beschreibung?, menge, einheit, preis_brutto_eur,
    mwst_prozent?. Gibt (line_items, gesamt_brutto, fehler|None) zurueck.
    """
    from core.integrations.accounting_base import InvoiceLineItem

    line_items = []
    gesamt = Decimal("0")
    for i, p in enumerate(positionen, start=1):
        name = (p.get("name") or "").strip()
        if not name:
            continue
        try:
            menge = Decimal(str(p.get("menge") or 1))
            preis = Decimal(str(p.get("preis_brutto_eur") or 0))
        except Exception:  # noqa: BLE001
            return None, None, f"Position {i}: ungueltige Zahl."
        einheit = (p.get("einheit") or "Stueck").strip() or "Stueck"
        mwst = int(p.get("mwst_prozent") or 19)
        besch = (p.get("beschreibung") or "").strip() or None
        line_items.append(InvoiceLineItem(
            name=name, quantity=float(menge), unit_name=einheit,
            unit_price_gross=float(preis), description=besch,
            tax_rate_percent=mwst,
        ))
        gesamt += menge * preis
    return line_items, gesamt, None


# ---------------------------------------------------------------------------
# Angebot anlegen (DB + Lexware-Draft)
# ---------------------------------------------------------------------------

async def create_angebot(
    tid: uuid.UUID, *, kunde_name: str, positionen: list[dict],
    kunde_strasse: str | None = None, kunde_plz: str | None = None,
    kunde_ort: str | None = None, kunde_email: str | None = None,
    intro_text: str | None = None, remark_text: str | None = None,
    quelle: str = "web",
    assigned_employee_id: uuid.UUID | None = None,
) -> dict:
    """Legt Angebot + Positionen in der DB an und erstellt einen
    Lexware-Quotation-Draft. Spiegelt die fruehere Inline-Logik der Route."""
    from core.database.connection import get_session
    from sqlalchemy import select
    from core.models.angebot import (
        Angebot, ANGEBOT_STATUS_ERSTELLT, ANGEBOT_STATUS_IN_LEXWARE)
    from core.models.angebot_position import AngebotPosition

    kunde_name = (kunde_name or "").strip()
    if not kunde_name:
        return {"ok": False, "error": "Kundenname ist Pflicht."}
    if not positionen:
        return {"ok": False, "error": "Mindestens 1 Position."}
    line_items, gesamt, err = _positionen_to_line_items(positionen)
    if err:
        return {"ok": False, "error": err}
    if not line_items:
        return {"ok": False, "error": "Mindestens 1 gueltige Position."}

    async with get_session() as s:
        ang = Angebot(
            tenant_id=tid, quelle=quelle, raw_input=None,
            kunde_name=kunde_name,
            kunde_strasse=(kunde_strasse or "").strip() or None,
            kunde_plz=(kunde_plz or "").strip() or None,
            kunde_ort=(kunde_ort or "").strip() or None,
            kunde_email=(kunde_email or "").strip() or None,
            introduction_text=(intro_text or "").strip() or None,
            remark_text=(remark_text or "").strip() or None,
            status=ANGEBOT_STATUS_ERSTELLT,
            assigned_employee_id=assigned_employee_id,
        )
        s.add(ang)
        await s.flush()
        from core.services.kunde_identity import (
            compose_adresse, resolve_kunde_id_safe)
        ang.kunde_id = await resolve_kunde_id_safe(
            s, tid, kunde_name, email=kunde_email,
            adresse=compose_adresse(kunde_strasse, kunde_plz, kunde_ort),
        )
        for i, p in enumerate(positionen, start=1):
            name = (p.get("name") or "").strip()
            if not name:
                continue
            try:
                menge = Decimal(str(p.get("menge") or 1))
                preis = Decimal(str(p.get("preis_brutto_eur") or 0))
            except Exception:  # noqa: BLE001
                continue
            s.add(AngebotPosition(
                angebot_id=ang.id, position_nr=i, name=name,
                beschreibung=(p.get("beschreibung") or "").strip() or None,
                menge=menge, einheit=(p.get("einheit") or "Stueck").strip() or "Stueck",
                preis_brutto_eur=preis, mwst_prozent=int(p.get("mwst_prozent") or 19),
            ))
        ang.gesamtbetrag_brutto_eur = gesamt
        await s.commit()
        await s.refresh(ang)
        ang_id = ang.id

    provider = await _lexware_provider(tid)
    if provider is None:
        return {"ok": True, "id": str(ang_id), "kunde": kunde_name,
                "gesamt_brutto_eur": float(gesamt), "lexware_voucher_number": None,
                "warning": "Lexware nicht verbunden — Angebot nur lokal gespeichert."}

    one_time_address = {
        "name": kunde_name, "street": kunde_strasse or "",
        "zip": kunde_plz or "", "city": kunde_ort or "", "countryCode": "DE"}
    try:
        quotation = await provider.create_quotation_draft(
            line_items=line_items, one_time_address=one_time_address,
            title=f"Angebot {kunde_name}",
            introduction=(intro_text or "").strip() or
                f"Sehr geehrte/r {kunde_name},\n\nvielen Dank fuer Ihre Anfrage. "
                "Wir freuen uns, Ihnen folgendes Angebot zu unterbreiten.",
            remark=(remark_text or "").strip() or
                "Die Preise verstehen sich inkl. gesetzlicher MwSt.",
            tax_type="gross")
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_angebot Lexware-quotation crash: %s", exc)
        return {"ok": True, "id": str(ang_id), "kunde": kunde_name,
                "gesamt_brutto_eur": float(gesamt), "lexware_voucher_number": None,
                "warning": f"Lexware-Fehler: {str(exc)[:200]}"}

    async with get_session() as s:
        a = (await s.execute(select(Angebot).where(Angebot.id == ang_id))).scalar_one()
        a.lexware_quotation_id = quotation.quotation_id
        a.lexware_voucher_number = quotation.voucher_number
        a.status = ANGEBOT_STATUS_IN_LEXWARE
        await s.commit()

    return {"ok": True, "id": str(ang_id), "kunde": kunde_name,
            "gesamt_brutto_eur": float(gesamt),
            "lexware_voucher_number": quotation.voucher_number,
            "lexware_deeplink": quotation.deeplink_view}


# ---------------------------------------------------------------------------
# Auftrag von Hand anlegen (nur DB, KEIN Lexware-Draft)
# ---------------------------------------------------------------------------

# Feldlaengen der angebote-/angebot_positionen-Spalten. Hier gekappt statt
# der DB einen 300-Zeichen-Namen hinzuwerfen (der knallt sonst als 500).
_MAX_KUNDE_NAME = 300
_MAX_EMAIL = 255
_MAX_STRASSE = 300
_MAX_PLZ = 20
_MAX_ORT = 200
_MAX_POS_NAME = 500
_MAX_EINHEIT = 50


def _kurz(text: str | None, maxlen: int) -> str | None:
    t = (text or "").strip()
    return t[:maxlen] or None


async def create_auftrag_manuell(
    tid: uuid.UUID, *, kunde_name: str, positionen: list[dict],
    status: str | None = None,
    kunde_strasse: str | None = None, kunde_plz: str | None = None,
    kunde_ort: str | None = None, kunde_email: str | None = None,
    quelle: str = "manuell",
    assigned_employee_id: uuid.UUID | None = None,
) -> dict:
    """Legt einen Auftrag direkt in der Auftragsliste an — ohne Angebot und
    ohne Lexware-Draft. Fuer Arbeit, die am Telefon oder auf der Baustelle
    vereinbart wurde und nie durch die Angebots-Pipeline gelaufen ist.

    Der Auftrag ist trotzdem vollwertig: die Rechnung am Ende entsteht in
    ``finalize_and_send_invoice`` aus den Positionen und nicht aus dem
    Lexware-Angebot. Ein handangelegter Auftrag laeuft also bis zum
    Rechnungsversand und ins Drive-Archiv wie jeder andere.

    ``status`` ist der Schritt, an dem der Auftrag startet (Default:
    angenommen — von Hand angelegt heisst in der Praxis "Kunde hat zugesagt").
    Erlaubt sind die Lifecycle-Schritte AUSSER ``rechnung_gesendet``: der
    schliesst den Auftrag ab und loest Rechnung + Archiv aus (Geld-Pfad),
    das darf eine Neuanlage nicht ueberspringen.
    """
    from core.database.connection import get_session
    from core.models.angebot import (
        Angebot, ANGEBOT_STATUS_ACCEPTED, ANGEBOT_STATUS_RECHNUNG_GESENDET,
        AUFTRAG_LIFECYCLE)
    from core.models.angebot_position import AngebotPosition

    kunde_name = (kunde_name or "").strip()
    if not kunde_name:
        return {"ok": False, "error": "Kundenname ist Pflicht."}

    status = (status or "").strip() or ANGEBOT_STATUS_ACCEPTED
    if status == ANGEBOT_STATUS_RECHNUNG_GESENDET or status not in AUFTRAG_LIFECYCLE:
        return {"ok": False, "error": "Dieser Startschritt ist nicht erlaubt."}

    if not positionen:
        return {"ok": False, "error": "Mindestens 1 Position."}
    _items, gesamt, err = _positionen_to_line_items(positionen)
    if err:
        return {"ok": False, "error": err}
    if not _items:
        return {"ok": False, "error": "Mindestens 1 gueltige Position."}

    async with get_session() as s:
        ang = Angebot(
            tenant_id=tid, quelle=quelle, raw_input=None,
            kunde_name=kunde_name[:_MAX_KUNDE_NAME],
            kunde_strasse=_kurz(kunde_strasse, _MAX_STRASSE),
            kunde_plz=_kurz(kunde_plz, _MAX_PLZ),
            kunde_ort=_kurz(kunde_ort, _MAX_ORT),
            kunde_email=_kurz(kunde_email, _MAX_EMAIL),
            status=status,
            gesamtbetrag_brutto_eur=gesamt,
            assigned_employee_id=assigned_employee_id,
        )
        # Ab "angenommen" gehoert die Zusage des Kunden zur Geschichte des
        # Auftrags — die Detailansicht zeigt sie als "angenommen am" an.
        if AUFTRAG_LIFECYCLE.index(status) >= AUFTRAG_LIFECYCLE.index(
                ANGEBOT_STATUS_ACCEPTED):
            import datetime as _dt
            ang.accepted_at = _dt.datetime.now(_dt.timezone.utc)
        s.add(ang)
        await s.flush()
        from core.services.kunde_identity import (
            compose_adresse, resolve_kunde_id_safe)
        ang.kunde_id = await resolve_kunde_id_safe(
            s, tid, kunde_name, email=kunde_email,
            adresse=compose_adresse(kunde_strasse, kunde_plz, kunde_ort),
        )
        for i, p in enumerate(positionen, start=1):
            name = (p.get("name") or "").strip()
            if not name:
                continue
            try:
                menge = Decimal(str(p.get("menge") or 1))
                preis = Decimal(str(p.get("preis_brutto_eur") or 0))
            except Exception:  # noqa: BLE001
                continue
            s.add(AngebotPosition(
                angebot_id=ang.id, position_nr=i, name=name[:_MAX_POS_NAME],
                beschreibung=(p.get("beschreibung") or "").strip() or None,
                menge=menge,
                einheit=_kurz(p.get("einheit"), _MAX_EINHEIT) or "Stueck",
                preis_brutto_eur=preis, mwst_prozent=int(p.get("mwst_prozent") or 19),
            ))
        await s.commit()
        ang_id = ang.id

    logger.info("Auftrag von Hand angelegt: id=%s tenant=%s status=%s quelle=%s",
                ang_id, tid, status, quelle)
    return {"ok": True, "id": str(ang_id), "kunde": kunde_name,
            "status": status, "gesamt_brutto_eur": float(gesamt)}


# ---------------------------------------------------------------------------
# Rechnung anlegen (DB + Lexware-Draft)
# ---------------------------------------------------------------------------

async def create_rechnung(
    tid: uuid.UUID, *, kunde_name: str,
    positionen: list[dict] | None = None,
    leistung_titel: str | None = None, leistung_beschreibung: str | None = None,
    betrag_brutto_eur=None,
    kunde_strasse: str | None = None, kunde_plz: str | None = None,
    kunde_ort: str | None = None, kunde_email: str | None = None,
    input_type: str = "web",
) -> dict:
    """Legt eine Rechnung (DB + Lexware-Draft) an. Pauschal-Modus
    (leistung_titel + betrag_brutto_eur) ODER Positionen-Modus."""
    from core.database.connection import get_session
    from sqlalchemy import select
    from core.models.rechnung import (
        Rechnung, RECHNUNG_STATUS_DRAFTED, RECHNUNG_STATUS_EXTRACTING)
    from core.integrations.accounting_base import InvoiceLineItem

    kunde_name = (kunde_name or "").strip()
    if not kunde_name:
        return {"ok": False, "error": "Kundenname ist Pflicht."}

    leistung_titel = (leistung_titel or "").strip()
    leistung_beschr = (leistung_beschreibung or "").strip() or None
    line_items: list[InvoiceLineItem] = []
    if betrag_brutto_eur and leistung_titel:
        try:
            betrag = Decimal(str(betrag_brutto_eur))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "Betrag ungueltig."}
        line_items.append(InvoiceLineItem(
            name=leistung_titel, quantity=1.0, unit_name="Stueck",
            unit_price_gross=float(betrag), description=leistung_beschr,
            tax_rate_percent=19))
    elif positionen:
        items, _g, err = _positionen_to_line_items(positionen)
        if err:
            return {"ok": False, "error": err}
        line_items = items
    else:
        return {"ok": False, "error": "Entweder Pauschal-Betrag oder Positionen angeben."}
    if not line_items:
        return {"ok": False, "error": "Mindestens 1 Position."}

    betrag_gesamt = sum(
        Decimal(str(li.quantity)) * Decimal(str(li.unit_price_gross)) for li in line_items)

    async with get_session() as s:
        r = Rechnung(
            tenant_id=tid, input_type=input_type, raw_input_text=None,
            kunde_name=kunde_name,
            kunde_strasse=(kunde_strasse or "").strip() or None,
            kunde_plz=(kunde_plz or "").strip() or None,
            kunde_ort=(kunde_ort or "").strip() or None,
            kunde_email=(kunde_email or "").strip() or None,
            leistung_titel=leistung_titel or line_items[0].name,
            leistung_beschreibung=leistung_beschr,
            betrag_brutto_eur=betrag_gesamt,
            status=RECHNUNG_STATUS_EXTRACTING)
        s.add(r)
        from core.services.kunde_identity import (
            compose_adresse, resolve_kunde_id_safe)
        r.kunde_id = await resolve_kunde_id_safe(
            s, tid, kunde_name, email=kunde_email,
            adresse=compose_adresse(kunde_strasse, kunde_plz, kunde_ort),
        )
        await s.flush()
        # Positionen mitschreiben. Bisher blieb nur die Summe uebrig — die
        # Rechnung liess sich damit spaeter nicht originalgetreu in Lexware
        # ausstellen, und in der App war nie zu sehen, woraus sie besteht.
        from core.models.rechnung_position import RechnungPosition
        for nr, li in enumerate(line_items, start=1):
            s.add(RechnungPosition(
                rechnung_id=r.id, position_nr=nr, name=li.name,
                beschreibung=li.description,
                menge=Decimal(str(li.quantity)),
                einheit=li.unit_name or "Stueck",
                preis_brutto_eur=Decimal(str(li.unit_price_gross)),
                mwst_prozent=int(li.tax_rate_percent or 19)))
        await s.commit()
        await s.refresh(r)
        rid = r.id

    provider = await _lexware_provider(tid)
    if provider is None:
        return {"ok": True, "id": str(rid), "kunde": kunde_name,
                "betrag_brutto_eur": float(betrag_gesamt),
                "warning": "Lexware nicht verbunden — Rechnung nur lokal gespeichert."}

    one_time_address = {
        "name": kunde_name, "street": kunde_strasse or "",
        "zip": kunde_plz or "", "city": kunde_ort or "", "countryCode": "DE"}
    try:
        invoice = await provider.create_invoice_draft(
            line_items=line_items, one_time_address=one_time_address,
            title=f"Rechnung {kunde_name}",
            introduction=f"Sehr geehrte/r {kunde_name},\n\nvielen Dank fuer Ihren Auftrag.",
            remark="Vielen Dank fuer Ihren Auftrag!", tax_type="gross")
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_rechnung Lexware-invoice crash: %s", exc)
        return {"ok": True, "id": str(rid), "kunde": kunde_name,
                "betrag_brutto_eur": float(betrag_gesamt),
                "warning": f"Lexware-Fehler: {str(exc)[:200]}"}

    async with get_session() as s:
        rr = (await s.execute(select(Rechnung).where(Rechnung.id == rid))).scalar_one()
        rr.lexware_invoice_id = invoice.invoice_id
        rr.lexware_voucher_number = invoice.voucher_number
        rr.status = RECHNUNG_STATUS_DRAFTED
        await s.commit()

    return {"ok": True, "id": str(rid), "kunde": kunde_name,
            "betrag_brutto_eur": float(betrag_gesamt),
            "lexware_voucher_number": invoice.voucher_number,
            "lexware_deeplink": invoice.deeplink_view}


# ---------------------------------------------------------------------------
# Angebot versenden
# ---------------------------------------------------------------------------

async def _angebot_finalisieren(tid: uuid.UUID, *, angebot_id: uuid.UUID) -> dict:
    """Macht aus dem Lexware-Entwurf ein ausgestelltes Angebot.

    Lexware kennt keine Umwandlung 'draft -> ausgestellt'; wie beim
    Rechnungsweg legen wir das Dokument deshalb neu und direkt finalisiert
    an — und loeschen den Entwurf danach selbst, statt ihn liegen zu lassen.

    Ist das Angebot schon ausgestellt, passiert nichts (idempotent). Ohne
    Lexware-Entwurf ebenfalls nicht — dann laeuft der bisherige Weg.
    """
    from sqlalchemy import select

    from core.database.connection import get_session
    from core.integrations.accounting_base import InvoiceLineItem
    from core.models.angebot import Angebot
    from core.models.angebot_position import AngebotPosition

    provider = await _lexware_provider(tid)
    if provider is None:
        return {"ok": False, "error": "Lexware ist nicht eingerichtet."}

    async with get_session() as s:
        ang = (await s.execute(
            select(Angebot).where(Angebot.id == angebot_id)
            .where(Angebot.tenant_id == tid))).scalar_one_or_none()
        if ang is None:
            return {"ok": False, "error": "Angebot nicht gefunden."}
        if not ang.lexware_quotation_id:
            return {"ok": True, "unveraendert": True}
        positions = (await s.execute(
            select(AngebotPosition)
            .where(AngebotPosition.angebot_id == angebot_id)
            .order_by(AngebotPosition.position_nr))).scalars().all()
        alter_entwurf = ang.lexware_quotation_id
        kunde_name = ang.kunde_name or ""
        kunde_strasse, kunde_plz, kunde_ort = (
            ang.kunde_strasse, ang.kunde_plz, ang.kunde_ort)
        intro = ang.introduction_text or None
        schluss = ang.remark_text or None

    try:
        quote = await provider.get_quotation(alter_entwurf)
        if "draft" not in (quote.get("voucherStatus") or "").lower():
            return {"ok": True, "unveraendert": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Angebot-Finalisierung: get_quotation fehlgeschlagen: %s", exc)
        return {"ok": True, "unveraendert": True}   # bisherigen Weg gehen lassen

    if not positions:
        return {"ok": False, "error": "Angebot hat keine Positionen."}

    line_items = [
        InvoiceLineItem(
            name=p.name, quantity=float(p.menge), unit_name=p.einheit or "Stueck",
            unit_price_gross=float(p.preis_brutto_eur), description=p.beschreibung,
            tax_rate_percent=int(p.mwst_prozent or 19))
        for p in positions]
    one_time_address = {"name": kunde_name, "countryCode": "DE"}
    if kunde_strasse:
        one_time_address["street"] = kunde_strasse
    if kunde_plz:
        one_time_address["zip"] = kunde_plz
    if kunde_ort:
        one_time_address["city"] = kunde_ort

    try:
        neu = await provider.create_quotation_draft(
            line_items=line_items, one_time_address=one_time_address,
            title=f"Angebot {kunde_name}".strip(),
            introduction=intro, remark=schluss,
            tax_type="gross", finalize=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Angebot-Finalisierung fehlgeschlagen: %s", exc)
        return {"ok": False,
                "error": "Angebot konnte in Lexware nicht ausgestellt werden."}

    async with get_session() as s:
        a = (await s.execute(
            select(Angebot).where(Angebot.id == angebot_id))).scalar_one_or_none()
        if a is not None:
            a.lexware_quotation_id = neu.quotation_id
            if getattr(neu, "voucher_number", None):
                a.lexware_voucher_number = neu.voucher_number
            await s.commit()

    try:
        await provider.delete_voucher(alter_entwurf)
        logger.info("Angebot-Finalisierung: Entwurf %s geloescht", alter_entwurf)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Angebot-Finalisierung: Entwurf %s nicht loeschbar (%s)",
                       alter_entwurf, exc)

    return {"ok": True, "unveraendert": False,
            "quotation_id": str(neu.quotation_id)}


async def send_angebot(
    tid: uuid.UUID, *, angebot_id: uuid.UUID,
    to_email: str | None = None, cc: list[str] | None = None,
) -> dict:
    """Verschickt ein (in Lexware angelegtes) Angebot per Mail an den Kunden."""
    from core.database.connection import get_session
    from sqlalchemy import select
    from core.models.angebot import Angebot
    from core.integrations.angebot_mail import send_angebot_to_customer

    async with get_session() as s:
        ang = (await s.execute(
            select(Angebot).where(Angebot.id == angebot_id)
            .where(Angebot.tenant_id == tid))).scalar_one_or_none()
    if ang is None:
        return {"ok": False, "error": "Angebot nicht gefunden."}
    ziel = (to_email or "").strip() or ang.kunde_email
    if not ziel:
        return {"ok": False, "error": "Keine Empfaenger-Mail vorhanden."}

    # Angebote lagen bisher als Entwurf in Lexware, und der Versand brach mit
    # „bitte im Lexware-Web finalisieren" ab — ein Medienbruch bei JEDEM
    # Angebot. Wir machen es jetzt selbst fertig, in dem Moment, in dem der
    # Inhaber den Versand freigibt.
    fin = await _angebot_finalisieren(tid, angebot_id=angebot_id)
    if not fin.get("ok"):
        return {"ok": False, "error": fin.get("error") or "Angebot konnte nicht finalisiert werden."}

    try:
        result = await send_angebot_to_customer(angebot_id=angebot_id, to_email=ziel, cc=cc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("send_angebot crash: %s", exc)
        return {"ok": False, "error": "Mail-Versand fehlgeschlagen."}
    if not result.get("success"):
        return {"ok": False, "error": result.get("error") or "Mail-Versand fehlgeschlagen."}
    return {"ok": True, "kunde": ang.kunde_name, "to_email": ziel,
            "message_id": result.get("message_id")}


# ---------------------------------------------------------------------------
# Rechnung finalisieren + versenden (Auftrag abrechnen)
# ---------------------------------------------------------------------------

async def finalize_and_send_rechnung(
    tid: uuid.UUID,
    *,
    rechnung_id: uuid.UUID,
    to_email: str | None = None,
    cc: list[str] | None = None,
) -> dict:
    """Stellt eine im App-Formular erfasste Rechnung aus und schickt sie.

    Dieser Weg war strukturell tot: ``/rechnungen/anlegen`` erzeugte einen
    Lexware-Entwurf, und der Versand rief eine Funktion, die es fuer diesen
    Datentyp gar nicht gab — jeder Klick endete in „Bitte erst in Lexware
    finalisieren". Jetzt macht der Versand dasselbe wie beim Auftragsweg:
    finalisiert in Lexware (Lexware kennt kein 'Entwurf -> ausgestellt',
    also neu anlegen), raeumt den Entwurf weg, holt das PDF und mailt es.
    """
    import datetime as _dt

    from sqlalchemy import select

    from core.database.connection import get_session
    from core.integrations.accounting_base import InvoiceLineItem
    from core.models.rechnung import (
        Rechnung, RECHNUNG_STATUS_DRAFTED, RECHNUNG_STATUS_MAIL_SENT)
    from core.models.rechnung_position import RechnungPosition
    from core.models.tenant import Tenant

    async with get_session() as s:
        r = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id)
            .where(Rechnung.tenant_id == tid))).scalar_one_or_none()
        if r is None:
            return {"ok": False, "error": "Rechnung nicht gefunden."}
        positions = (await s.execute(
            select(RechnungPosition)
            .where(RechnungPosition.rechnung_id == rechnung_id)
            .order_by(RechnungPosition.position_nr))).scalars().all()
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid))).scalar_one_or_none()
        ziel = (to_email or "").strip() or r.kunde_email
        daten = {
            "kunde_name": r.kunde_name or "", "betrag": r.betrag_brutto_eur,
            "titel": r.leistung_titel or "Leistung",
            "beschreibung": r.leistung_beschreibung,
            "strasse": r.kunde_strasse, "plz": r.kunde_plz, "ort": r.kunde_ort,
            "alter_entwurf": r.lexware_invoice_id,
            "bezahlt_am": r.bezahlt_am,
        }

    if not ziel:
        return {"ok": False, "error": "Keine Empfaenger-Mail hinterlegt."}
    if daten["bezahlt_am"] is not None:
        return {"ok": False, "error": "Diese Rechnung ist bereits bezahlt."}

    provider = await _lexware_provider(tid)
    if provider is None:
        return {"ok": False, "error": "Lexware ist nicht eingerichtet."}

    if positions:
        line_items = [
            InvoiceLineItem(
                name=p.name, quantity=float(p.menge),
                unit_name=p.einheit or "Stueck",
                unit_price_gross=float(p.preis_brutto_eur),
                description=p.beschreibung,
                tax_rate_percent=int(p.mwst_prozent or 19))
            for p in positions]
    elif daten["betrag"]:
        # Aeltere Rechnungen haben keine gespeicherten Positionen — dann
        # eine Sammelposition aus Titel und Betrag.
        line_items = [InvoiceLineItem(
            name=daten["titel"], quantity=1.0, unit_name="Stueck",
            unit_price_gross=float(daten["betrag"]),
            description=daten["beschreibung"], tax_rate_percent=19)]
    else:
        return {"ok": False, "error": "Rechnung hat weder Positionen noch Betrag."}

    one_time_address = {"name": daten["kunde_name"], "countryCode": "DE"}
    if daten["strasse"]:
        one_time_address["street"] = daten["strasse"]
    if daten["plz"]:
        one_time_address["zip"] = daten["plz"]
    if daten["ort"]:
        one_time_address["city"] = daten["ort"]

    try:
        invoice = await provider.create_invoice_draft(
            line_items=line_items, one_time_address=one_time_address,
            title=f"Rechnung {daten['kunde_name']}".strip(),
            introduction=(
                f"Sehr geehrte/r {daten['kunde_name']},\n\nvielen Dank fuer "
                f"Ihren Auftrag."),
            remark="Bitte begleichen Sie den Rechnungsbetrag fristgerecht.",
            tax_type="gross", finalize=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("finalize_and_send_rechnung: Lexware: %s", exc)
        return {"ok": False,
                "error": "Rechnung konnte in Lexware nicht ausgestellt werden."}

    if daten["alter_entwurf"] and daten["alter_entwurf"] != invoice.invoice_id:
        try:
            await provider.delete_voucher(daten["alter_entwurf"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alten Rechnungs-Entwurf nicht loeschbar (%s)", exc)

    async with get_session() as s:
        rr = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id))).scalar_one_or_none()
        if rr is not None:
            rr.lexware_invoice_id = invoice.invoice_id
            if getattr(invoice, "voucher_number", None):
                rr.lexware_voucher_number = invoice.voucher_number
            rr.status = RECHNUNG_STATUS_DRAFTED
            await s.commit()

    # PDF holen und verschicken
    from core.integrations.angebot_mail import _build_rechnung_mail_html
    from core.integrations.microsoft import send_tracked_mail

    try:
        pdf_bytes = await provider.download_invoice_pdf(invoice.invoice_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("finalize_and_send_rechnung: PDF: %s", exc)
        return {"ok": False, "error": "Rechnungs-PDF konnte nicht geladen werden.",
                "lexware_ausgestellt": True}

    nummer = getattr(invoice, "voucher_number", None) or ""
    body_html = _build_rechnung_mail_html(
        kunde_anrede=daten["kunde_name"],
        rechnung_nummer=nummer,
        gesamtbetrag_brutto_eur=daten["betrag"],
        company_name=(tenant.company_name if tenant else "") or "",
        contact_name=(getattr(tenant, "contact_name", "") or ""),
        contact_email=(getattr(tenant, "contact_email", "") or ""),
        contact_phone=(getattr(tenant, "contact_phone", "") or ""))
    betreff = f"Ihre Rechnung{(' ' + nummer) if nummer else ''} von {(tenant.company_name if tenant else '') or 'uns'}"
    dateiname = f"Rechnung{('-' + nummer) if nummer else ''}.pdf"

    res = await send_tracked_mail(
        tenant_id=tid, to_email=ziel, subject=betreff, body_html=body_html,
        cc=cc, attachments=[{"filename": dateiname, "bytes": pdf_bytes,
                             "content_type": "application/pdf"}])

    if not res.get("success"):
        # Die Rechnung ist ausgestellt (steuerlich gezogen) — der Versand
        # darf nicht einfach verloren gehen.
        try:
            from core.integrations.mail_retry_cron import enqueue_failed_mail
            from core.models import MAIL_TYPE_RECHNUNG
            await enqueue_failed_mail(
                tenant_id=tid, mail_type=MAIL_TYPE_RECHNUNG,
                recipient_email=ziel, subject=betreff, html_body=body_html,
                attachments=[{"filename": dateiname,
                              "mime_type": "application/pdf",
                              "content_bytes": pdf_bytes}],
                from_name=(tenant.company_name if tenant else None),
                rechnung_id=rechnung_id, mail_backend="microsoft_graph",
                last_error=res.get("error") or "Mail-Versand fehlgeschlagen")
        except Exception as exc:  # noqa: BLE001
            logger.exception("enqueue_failed_mail (formular-rechnung): %s", exc)
        return {"ok": False, "error": res.get("error") or "Mail-Versand fehlgeschlagen.",
                "lexware_ausgestellt": True, "queued": True}

    async with get_session() as s:
        rr = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id))).scalar_one_or_none()
        if rr is not None:
            rr.status = RECHNUNG_STATUS_MAIL_SENT
            rr.mail_sent_at = _dt.datetime.now(_dt.timezone.utc)
            rr.mail_sent_to = ziel
            await s.commit()

    return {"ok": True, "to_email": ziel, "nummer": nummer,
            "deeplink": getattr(invoice, "deeplink_view", None),
            "kunde": daten["kunde_name"]}


async def _spiegle_rechnung(
    tid: uuid.UUID,
    *,
    angebot_id: uuid.UUID,
    invoice,
    kunde_name: str | None,
    kunde_email: str | None,
    kunde_strasse: str | None,
    kunde_plz: str | None,
    kunde_ort: str | None,
    positions,
) -> uuid.UUID | None:
    """Legt zu einer finalisierten Lexware-Rechnung die Zeile in `rechnungen` an.

    Hintergrund: es gab zwei Rechnungswelten. Der Weg ueber den fertigen
    Auftrag (dieser hier) schrieb nur das Angebot fort, die Auswertungen
    lesen aber `rechnungen` — verschickte Rechnungen tauchten deshalb weder
    in den offenen Posten noch in der Bezahl-Ueberwachung auf.

    Natuerlicher Schluessel ist `lexware_invoice_id`: ein zweiter Aufruf zur
    selben Lexware-Rechnung aktualisiert die Zeile, statt eine zweite
    anzulegen. Fehler hier duerfen den Rechnungsversand nicht kippen — die
    Rechnung liegt zu diesem Zeitpunkt bereits finalisiert in Lexware.
    """
    from sqlalchemy import select

    from core.database.connection import get_session
    from core.models.angebot import Angebot
    from core.models.rechnung import Rechnung, RECHNUNG_STATUS_DRAFTED

    try:
        betrag = sum(
            float(p.menge or 0) * float(p.preis_brutto_eur or 0)
            for p in positions)
    except Exception:  # noqa: BLE001
        betrag = None

    try:
        async with get_session() as s:
            vorhanden = (await s.execute(
                select(Rechnung)
                .where(Rechnung.tenant_id == tid)
                .where(Rechnung.lexware_invoice_id == invoice.invoice_id)
            )).scalar_one_or_none()

            r = vorhanden or Rechnung(
                tenant_id=tid,
                input_type="auftrag",   # entstanden aus einem fertigen Auftrag
                status=RECHNUNG_STATUS_DRAFTED,
            )
            r.kunde_name = kunde_name
            r.kunde_email = kunde_email
            r.kunde_strasse = kunde_strasse
            r.kunde_plz = kunde_plz
            r.kunde_ort = kunde_ort
            if betrag:
                r.betrag_brutto_eur = round(betrag, 2)
            r.lexware_invoice_id = invoice.invoice_id
            if getattr(invoice, "voucher_number", None):
                r.lexware_voucher_number = invoice.voucher_number
            r.leistung_titel = f"Auftrag {kunde_name}" if kunde_name else "Auftrag"
            r.raw_input_text = f"aus Auftrag {angebot_id}"
            if vorhanden is None:
                s.add(r)
            await s.flush()
            # `Angebot.rechnung_id` gab es im Modell schon, wurde aber nie
            # beschrieben. Ab jetzt haengen Auftrag und Rechnung aneinander —
            # sonst waeren es weiterhin zwei Welten ohne Verbindung.
            a = (await s.execute(
                select(Angebot).where(Angebot.id == angebot_id))).scalar_one_or_none()
            if a is not None:
                a.rechnung_id = r.id
            await s.commit()
            return r.id
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Rechnungs-Spiegel fehlgeschlagen (tenant=%s angebot=%s): %s",
            tid, angebot_id, exc)
        return None


async def _rechnung_als_versendet_markieren(
    rechnung_id: uuid.UUID | None, kunde_email: str | None,
) -> None:
    """Setzt die gespiegelte Rechnung auf `mail_sent` — erst damit greift die
    Bezahl-Ueberwachung (sie pollt genau diesen Status)."""
    if rechnung_id is None:
        return
    import datetime as _dt

    from sqlalchemy import select

    from core.database.connection import get_session
    from core.models.rechnung import Rechnung, RECHNUNG_STATUS_MAIL_SENT

    try:
        async with get_session() as s:
            r = (await s.execute(
                select(Rechnung).where(Rechnung.id == rechnung_id)
            )).scalar_one_or_none()
            if r is None:
                return
            r.status = RECHNUNG_STATUS_MAIL_SENT
            r.mail_sent_at = _dt.datetime.now(_dt.timezone.utc)
            r.mail_sent_to = kunde_email
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Rechnungs-Spiegel auf mail_sent setzen: %s", exc)


async def finalize_and_send_invoice(
    tid: uuid.UUID,
    *,
    angebot_id: uuid.UUID,
    anschreiben: str | None = None,
    kunde_email_override: str | None = None,
) -> dict:
    """Finalisiert die Rechnung eines fertigen Auftrags in Lexware und
    schickt sie als PDF an den Kunden. Faktorisiert aus der frueheren
    Pipeline ``_run_rechnung_versand_pipeline`` — EINE Quelle der Wahrheit.

    ``anschreiben`` (optional): überschreibt die Einleitung auf der Rechnung
    (vom Handwerker in Q editiert). ``kunde_email_override`` (optional):
    abweichende Empfänger-Adresse.

    Strategie (wie bisher): Lexware kennt keine 'draft -> open'-Konvertierung,
    daher wird die Invoice NEU finalized angelegt; der alte Draft kann manuell
    geloescht werden.

    Returns dict mit: ok, error?, invoice_deeplink?, email_used?,
    email_from_lexware, mail_sent, mail_error?, status, kunde.
    """
    from core.database.connection import get_session
    from sqlalchemy import select
    from core.models.angebot import (
        Angebot, ANGEBOT_STATUS_WORK_DONE, ANGEBOT_STATUS_RECHNUNG_GESENDET)
    from core.models.angebot_position import AngebotPosition
    from core.integrations.accounting_base import InvoiceLineItem
    from core.integrations.angebot_mail import send_rechnung_to_customer

    async with get_session() as s:
        ang = (await s.execute(
            select(Angebot).where(Angebot.id == angebot_id)
            .where(Angebot.tenant_id == tid))).scalar_one_or_none()
        if ang is None:
            return {"ok": False, "error": "Auftrag nicht gefunden.", "status": None}
        positions = (await s.execute(
            select(AngebotPosition).where(AngebotPosition.angebot_id == angebot_id)
            .order_by(AngebotPosition.position_nr))).scalars().all()
        kunde_name = ang.kunde_name
        kunde_email = ang.kunde_email
        kunde_strasse, kunde_plz, kunde_ort = ang.kunde_strasse, ang.kunde_plz, ang.kunde_ort

    if kunde_email_override and kunde_email_override.strip():
        kunde_email = kunde_email_override.strip()

    provider = await _lexware_provider(tid)
    if provider is None:
        return {"ok": False, "error": "Lexware ist nicht eingerichtet.",
                "status": ANGEBOT_STATUS_WORK_DONE, "kunde": kunde_name}

    line_items = [
        InvoiceLineItem(
            name=p.name, quantity=float(p.menge), unit_name=p.einheit or "Stueck",
            unit_price_gross=float(p.preis_brutto_eur), description=p.beschreibung,
            tax_rate_percent=int(p.mwst_prozent or 19))
        for p in positions]
    one_time_address = {"name": kunde_name, "countryCode": "DE"}
    if kunde_strasse:
        one_time_address["street"] = kunde_strasse
    if kunde_plz:
        one_time_address["zip"] = kunde_plz
    if kunde_ort:
        one_time_address["city"] = kunde_ort

    intro_text = (anschreiben or "").strip() or (
        "Sehr geehrte Damen und Herren,\n\nvielen Dank fuer Ihren Auftrag "
        "und das entgegengebrachte Vertrauen. Wie vereinbart stellen wir "
        "Ihnen die erbrachten Leistungen nachstehend in Rechnung.")
    try:
        invoice = await provider.create_invoice_draft(
            line_items=line_items, one_time_address=one_time_address,
            title=f"Rechnung {kunde_name}", introduction=intro_text,
            remark="Bitte begleichen Sie den Rechnungsbetrag innerhalb von 14 Tagen.",
            tax_type="gross", finalize=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("finalize_and_send_invoice Finalisierung gescheitert: %s", exc)
        async with get_session() as s:
            a = (await s.execute(select(Angebot).where(Angebot.id == angebot_id))).scalar_one()
            a.status = ANGEBOT_STATUS_WORK_DONE
            await s.commit()
        return {"ok": False, "error": str(exc)[:200],
                "status": ANGEBOT_STATUS_WORK_DONE, "kunde": kunde_name}

    async with get_session() as s:
        a = (await s.execute(select(Angebot).where(Angebot.id == angebot_id))).scalar_one()
        alter_draft = a.lexware_invoice_id
        a.lexware_invoice_id = invoice.invoice_id
        a.status = ANGEBOT_STATUS_WORK_DONE
        await s.commit()

    # Der alte, nie versendete Entwurf aus der Angebots-Pipeline lag bisher
    # als Leiche in Lexware und musste von Hand geloescht werden. Lexware
    # kennt kein 'draft -> offen', wir legen also weiterhin neu an — raeumen
    # den Vorgaenger aber selbst weg. Scheitert das, ist es kein Grund, den
    # Versand abzubrechen: dann liegt dort eben ein Entwurf zu viel.
    if alter_draft and alter_draft != invoice.invoice_id:
        try:
            await provider.delete_voucher(alter_draft)
            logger.info("finalize_and_send_invoice: alten Entwurf %s geloescht",
                        alter_draft)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize_and_send_invoice: alter Entwurf %s nicht loeschbar "
                "(%s) — bitte in Lexware pruefen", alter_draft, exc)

    # Ab hier gibt es die Rechnung auch als eigene Zeile. Vorher lebte sie
    # ausschliesslich am Angebot, und damit sah KEINE der Geld-Auswertungen
    # sie je: offene Posten, Ueberfaelligkeit, Bezahl-Monitor, Tagesbericht
    # und Zahlungserinnerung lesen alle die Tabelle `rechnungen`.
    rechnung_id = await _spiegle_rechnung(
        tid, angebot_id=angebot_id, invoice=invoice,
        kunde_name=kunde_name, kunde_email=kunde_email,
        kunde_strasse=kunde_strasse, kunde_plz=kunde_plz, kunde_ort=kunde_ort,
        positions=positions)

    # Email-Fallback aus Lexware-Kontakten
    email_from_lexware = False
    if not kunde_email and kunde_name and len(kunde_name.strip()) >= 3:
        try:
            contacts = await provider.search_contacts(kunde_name, customer_only=True, limit=5)
        except Exception:  # noqa: BLE001
            contacts = []
        chosen = None
        for c in contacts:
            if c.email and any(tok in (c.name or "").lower()
                               for tok in kunde_name.lower().split()):
                chosen = c.email
                break
        if not chosen:
            for c in contacts:
                if c.email:
                    chosen = c.email
                    break
        if chosen:
            kunde_email = chosen
            email_from_lexware = True
            async with get_session() as s:
                a2 = (await s.execute(select(Angebot).where(Angebot.id == angebot_id))).scalar_one()
                a2.kunde_email = chosen
                await s.commit()

    base = {"ok": True, "invoice_deeplink": invoice.deeplink_view,
            "email_used": kunde_email, "email_from_lexware": email_from_lexware,
            "kunde": kunde_name}

    if not kunde_email:
        return {**base, "mail_sent": False,
                "mail_error": "Keine Kunden-Mail vorhanden.",
                "status": ANGEBOT_STATUS_WORK_DONE}

    try:
        mail_result = await send_rechnung_to_customer(angebot_id=angebot_id, to_email=kunde_email)
    except Exception as exc:  # noqa: BLE001
        logger.exception("finalize_and_send_invoice Mail-Versand crash: %s", exc)
        mail_result = {"success": False, "error": str(exc)}

    if mail_result.get("success"):
        import datetime as _dt

        async with get_session() as s:
            a = (await s.execute(select(Angebot).where(Angebot.id == angebot_id))).scalar_one()
            a.status = ANGEBOT_STATUS_RECHNUNG_GESENDET
            a.abgeschlossen_am = _dt.datetime.now(_dt.timezone.utc)
            await s.commit()
        # Erst `mail_sent` bringt die Rechnung in die Bezahl-Ueberwachung
        # (rechnung_payment_monitor pollt genau diesen Status).
        await _rechnung_als_versendet_markieren(rechnung_id, kunde_email)
        # Rechnung raus = Auftrag abgeschlossen -> Drive-Archiv. Bewusst als
        # Hintergrund-Task: der Handwerker soll nicht auf ein Dutzend Drive-
        # Requests warten, und ein Drive-Problem darf einen erfolgreichen
        # Rechnungsversand nicht als Fehler aussehen lassen.
        from core.services.auftrag_archiv import archiviere_im_hintergrund
        archiviere_im_hintergrund(tid, angebot_id)
        return {**base, "mail_sent": True, "status": ANGEBOT_STATUS_RECHNUNG_GESENDET}
    return {**base, "mail_sent": False,
            "mail_error": mail_result.get("error", "unbekannt"),
            "status": ANGEBOT_STATUS_WORK_DONE}


# ---------------------------------------------------------------------------
# Anfrage beantworten (Mail-Reply mit Threading)
# ---------------------------------------------------------------------------

async def send_anfrage_reply(
    tid: uuid.UUID, *, conv_id: uuid.UUID, reply_text: str,
    employee_id: uuid.UUID | None = None, close: bool = False,
) -> dict:
    """Beantwortet eine Kundenanfrage (EmailConversation) per Mail, RFC-konform
    gethreaded. Faktorisiert aus api_anfrage_reply."""
    from core.database.connection import get_session
    from sqlalchemy import select
    from core.models.email_conversation import EmailConversation, STATE_CLOSED
    from core.integrations.microsoft import send_tracked_mail
    from core.integrations.mail_pipeline import (
        record_outbound_q_reply, set_conversation_state)

    reply_text = (reply_text or "").strip()
    if not reply_text:
        return {"ok": False, "error": "Leere Antwort."}

    async with get_session() as s:
        conv = (await s.execute(
            select(EmailConversation).where(EmailConversation.id == conv_id)
            .where(EmailConversation.tenant_id == tid))).scalar_one_or_none()
    if conv is None:
        return {"ok": False, "error": "Anfrage nicht gefunden."}
    if not conv.kunde_email:
        return {"ok": False, "error": "Keine Kunden-Mail in der Anfrage."}

    base_subject = (conv.last_subject or "Ihre Anfrage").strip()
    reply_subject = base_subject if base_subject.lower().startswith("re:") else f"Re: {base_subject}"
    paragraphs = [p.strip() for p in reply_text.split("\n\n") if p.strip()]
    body_html = "".join("<p>" + p.replace("\n", "<br>") + "</p>" for p in paragraphs)

    # Threading: als Elternteil dient unsere letzte Mail in diesem Thread
    # (last_message_id). Die Message-ID der eingehenden Kunden-Mail halten
    # wir nicht vor — fuer die Zuordnung beim Empfaenger genuegt ein
    # gemeinsamer Vorfahre in der References-Kette, danach gruppieren die
    # gaengigen Clients korrekt.
    try:
        send_result = await send_tracked_mail(
            tenant_id=tid, to_email=conv.kunde_email, subject=reply_subject,
            body_html=body_html, body_text=reply_text, employee_id=employee_id,
            in_reply_to=conv.last_message_id or None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("send_anfrage_reply send_tracked_mail crash: %s", exc)
        return {"ok": False, "error": "Mail-Versand fehlgeschlagen."}
    if not send_result.get("success"):
        return {"ok": False, "error": send_result.get("error") or "Mail-Versand fehlgeschlagen."}

    await record_outbound_q_reply(
        conv_id=conv_id,
        internet_message_id=send_result.get("internet_message_id"),
        microsoft_conversation_id=send_result.get("conversation_id"),
        q_reply_text=reply_text, subject=reply_subject)
    if close:
        await set_conversation_state(conv_id, STATE_CLOSED)

    return {"ok": True, "kunde": conv.kunde_name or conv.kunde_email,
            "to_email": conv.kunde_email,
            "internet_message_id": send_result.get("internet_message_id"),
            "closed": close}


# ---------------------------------------------------------------------------
# Lookups fuer den Assistenten (Entity-Aufloesung per Kundenname)
# ---------------------------------------------------------------------------

async def find_angebot_for_send(tid: uuid.UUID, kunde_name: str):
    """Findet das juengste versendbare Angebot eines Kunden (in Lexware
    angelegt, noch nicht versendet/abgebrochen). Returns Angebot | 'AMBIG' | None."""
    from core.database.connection import get_session
    from sqlalchemy import select
    from core.models.angebot import (
        Angebot, ANGEBOT_STATUS_IN_LEXWARE, ANGEBOT_STATUS_ERSTELLT)

    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot).where(Angebot.tenant_id == tid)
            .where(Angebot.kunde_name.ilike(f"%{kunde_name.strip()}%"))
            .where(Angebot.status.in_([ANGEBOT_STATUS_IN_LEXWARE, ANGEBOT_STATUS_ERSTELLT]))
            .order_by(Angebot.created_at.desc()).limit(2))).scalars().all()
    if not rows:
        return None
    if len(rows) > 1:
        return "AMBIG"
    return rows[0]


async def find_auftrag_for_invoice(tid: uuid.UUID, kunde_name: str):
    """Findet den fertigen Auftrag (Angebot in arbeit_fertig) eines Kunden zum
    Abrechnen. Returns Angebot | 'AMBIG' | None."""
    from core.database.connection import get_session
    from sqlalchemy import select
    from core.models.angebot import Angebot, ANGEBOT_STATUS_WORK_DONE

    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot).where(Angebot.tenant_id == tid)
            .where(Angebot.kunde_name.ilike(f"%{kunde_name.strip()}%"))
            .where(Angebot.status == ANGEBOT_STATUS_WORK_DONE)
            .order_by(Angebot.created_at.desc()).limit(2))).scalars().all()
    if not rows:
        return None
    if len(rows) > 1:
        return "AMBIG"
    return rows[0]


async def find_open_conversation(tid: uuid.UUID, kunde_name: str):
    """Findet die offene Anfrage (EmailConversation) eines Kunden. Returns
    EmailConversation | 'AMBIG' | None."""
    from core.database.connection import get_session
    from sqlalchemy import select, or_
    from core.models.email_conversation import EmailConversation, STATE_CLOSED

    needle = f"%{kunde_name.strip()}%"
    async with get_session() as s:
        rows = (await s.execute(
            select(EmailConversation).where(EmailConversation.tenant_id == tid)
            .where(EmailConversation.state != STATE_CLOSED)
            .where(or_(EmailConversation.kunde_name.ilike(needle),
                       EmailConversation.kunde_email.ilike(needle)))
            .order_by(EmailConversation.updated_at.desc()).limit(2))).scalars().all()
    if not rows:
        return None
    if len(rows) > 1:
        return "AMBIG"
    return rows[0]
