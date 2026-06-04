"""Beleg-Fluss des Kundenzyklus als wiederverwendbarer Service.

Hier liegt die EINE Implementierung von Angebot/Rechnung anlegen + versenden
und Anfrage-Antwort. Genutzt von:
  * den PWA-App-Routen (core/api/app_screens.py),
  * dem Gemini-Assistenten (core/ai/command_center.py),
  * dem Telegram-Rechnungsversand (_run_rechnung_versand_pipeline).

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
        )
        s.add(ang)
        await s.flush()
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

async def finalize_and_send_invoice(tid: uuid.UUID, *, angebot_id: uuid.UUID) -> dict:
    """Finalisiert die Rechnung eines fertigen Auftrags in Lexware und
    schickt sie als PDF an den Kunden. Faktorisiert aus der Telegram-
    Pipeline ``_run_rechnung_versand_pipeline`` — EINE Quelle der Wahrheit.

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

    intro_text = (
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
        a.lexware_invoice_id = invoice.invoice_id
        a.status = ANGEBOT_STATUS_WORK_DONE
        await s.commit()

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
        async with get_session() as s:
            a = (await s.execute(select(Angebot).where(Angebot.id == angebot_id))).scalar_one()
            a.status = ANGEBOT_STATUS_RECHNUNG_GESENDET
            await s.commit()
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

    try:
        send_result = await send_tracked_mail(
            tenant_id=tid, to_email=conv.kunde_email, subject=reply_subject,
            body_html=body_html, body_text=reply_text, employee_id=employee_id)
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
