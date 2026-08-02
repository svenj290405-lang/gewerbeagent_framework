"""Zahlungserinnerung und Angebots-Nachfassen — Entwurf schreiben, senden.

Der Buchhaltungs-Bereich zeigt seit 2026-08 an, welche Rechnung ueberfaellig
ist und welches Angebot keine Antwort bekommen hat. Machen musste man
danach immer noch alles selbst. Hier entsteht der Text dazu.

Bewusst zweistufig wie ueberall im Geld-Pfad: ``entwurf()`` formuliert,
der Mensch liest und aendert, ``senden()`` verschickt. Und bewusst als
freundliche Mail, NICHT als Lexware-Mahnung: eine Mahnung ist ein Beleg
mit Rechtswirkung, der in den Buechern landet und Mahngebuehren traegt.
Die erste Erinnerung ist ueblicherweise gar keine Mahnung, sondern ein
"ist bei Ihnen vielleicht untergegangen?". Der foermliche Weg bleibt in
Lexware, wo er hingehoert.

Toene:
  ``freundlich``  – Erinnerung, evtl. uebersehen (Standard)
  ``bestimmt``    – klare Zahlungsaufforderung mit Frist
  ``letzte``      – letzte Frist vor dem foermlichen Weg
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

logger = logging.getLogger(__name__)

TOENE = ("freundlich", "bestimmt", "letzte")

_TON_ANWEISUNG = {
    "freundlich": (
        "Freundlich und ohne Vorwurf. Unterstelle, dass es untergegangen ist. "
        "Keine Frist, keine Konsequenzen, keine Gebuehren."
    ),
    "bestimmt": (
        "Sachlich und klar. Nenne eine Zahlungsfrist von 7 Tagen ab heute. "
        "Bleibe hoeflich, drohe nicht mit Anwalt oder Inkasso."
    ),
    "letzte": (
        "Sachlich, knapp und ernst. Letzte Frist von 7 Tagen, danach folgt "
        "der foermliche Weg. Keine Beschimpfung, keine Drohkulisse, keine "
        "Bezifferung von Gebuehren oder Zinsen."
    ),
}


def _eur(v) -> str:
    return f"{float(v or 0):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


async def entwurf_zahlungserinnerung(
    tid: uuid.UUID, rechnung_id: uuid.UUID, *, ton: str = "freundlich",
) -> dict:
    """Formuliert die Erinnerung zu einer offenen Rechnung. Verschickt nichts."""
    from sqlalchemy import select

    from core.database.connection import get_session
    from core.models.rechnung import Rechnung
    from core.models.tenant import Tenant

    if ton not in TOENE:
        ton = "freundlich"

    async with get_session() as s:
        r = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id,
                                   Rechnung.tenant_id == tid)
        )).scalar_one_or_none()
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid))).scalar_one_or_none()
    if r is None:
        return {"ok": False, "error": "Rechnung nicht gefunden."}
    if r.bezahlt_am is not None:
        return {"ok": False, "error": "Diese Rechnung ist bereits bezahlt."}
    if not r.mail_sent_at:
        return {"ok": False,
                "error": "Diese Rechnung ist noch gar nicht beim Kunden — "
                         "erst senden, dann erinnern."}

    tage = max(0, (dt.datetime.now(dt.timezone.utc) - r.mail_sent_at).days)
    betrag = _eur(r.betrag_brutto_eur)
    nummer = r.lexware_voucher_number or ""
    kunde = r.kunde_name or "Kunde"
    betreff = (f"Zahlungserinnerung zu Rechnung {nummer}".strip()
               if nummer else "Zahlungserinnerung")

    text = await _formuliere(
        tid,
        f"Schreibe eine Zahlungserinnerung.\n"
        f"Kunde: {kunde}\n"
        f"Rechnungsnummer: {nummer or 'unbekannt'}\n"
        f"Betrag: {betrag}\n"
        f"Verschickt vor: {tage} Tagen\n"
        f"Absender/Betrieb: {getattr(tenant, 'company_name', '') or 'unser Betrieb'}\n",
        ton=_TON_ANWEISUNG[ton],
        fallback=(
            f"Guten Tag {kunde},\n\n"
            f"unsere Rechnung {nummer} über {betrag} vom Versand vor {tage} Tagen "
            f"ist bei uns noch offen. Vermutlich ist sie untergegangen — "
            f"schauen Sie bitte kurz nach?\n\n"
            f"Sollten Sie bereits gezahlt haben, betrachten Sie diese "
            f"Nachricht bitte als gegenstandslos."
        ),
    )
    return {
        "ok": True, "typ": "zahlung", "id": str(r.id),
        "kunde": kunde, "empfaenger": r.kunde_email or "",
        "betreff": betreff, "text": text,
        "betrag": betrag, "tage": tage, "ton": ton,
    }


async def entwurf_nachfassen(tid: uuid.UUID, angebot_id: uuid.UUID) -> dict:
    """Formuliert das Nachfassen zu einem Angebot ohne Rueckmeldung."""
    from sqlalchemy import select

    from core.database.connection import get_session
    from core.models.angebot import Angebot
    from core.models.tenant import Tenant

    async with get_session() as s:
        a = (await s.execute(
            select(Angebot).where(Angebot.id == angebot_id,
                                  Angebot.tenant_id == tid)
        )).scalar_one_or_none()
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid))).scalar_one_or_none()
    if a is None:
        return {"ok": False, "error": "Angebot nicht gefunden."}

    tage = max(0, (dt.datetime.now(dt.timezone.utc) - a.created_at).days)
    betrag = _eur(a.gesamtbetrag_brutto_eur)
    kunde = a.kunde_name or "Kunde"
    # Angebot fuehrt keinen Leistungstitel — falls das Modell spaeter einen
    # bekommt, wandert er von selbst in den Prompt.
    titel = (getattr(a, "leistung_titel", None) or "").strip()

    text = await _formuliere(
        tid,
        f"Schreibe eine kurze Nachfrage zu einem Angebot, auf das der Kunde "
        f"noch nicht geantwortet hat.\n"
        f"Kunde: {kunde}\n"
        f"Angebotssumme: {betrag}\n"
        f"Verschickt vor: {tage} Tagen\n"
        + (f"Leistung: {titel}\n" if titel else "")
        + f"Absender/Betrieb: {getattr(tenant, 'company_name', '') or 'unser Betrieb'}\n",
        ton=(
            "Freundlich, kurz, ohne Druck. Biete an, Fragen zu klaeren oder "
            "das Angebot anzupassen. Kein Rabatt von sich aus. Keine Frist."
        ),
        fallback=(
            f"Guten Tag {kunde},\n\n"
            f"vor {tage} Tagen haben wir Ihnen unser Angebot über {betrag} "
            f"geschickt. Ich wollte kurz nachfragen, ob Sie noch Fragen dazu "
            f"haben oder ob wir etwas anpassen sollen.\n\n"
            f"Melden Sie sich gerne — wir richten uns nach Ihnen."
        ),
    )
    return {
        "ok": True, "typ": "nachfass", "id": str(a.id),
        "kunde": kunde, "empfaenger": a.kunde_email or "",
        "betreff": f"Unser Angebot über {betrag}",
        "text": text, "betrag": betrag, "tage": tage,
    }


async def _formuliere(tid: uuid.UUID, fakten: str, *, ton: str, fallback: str) -> str:
    """Laesst Gemini den Text schreiben; bei jedem Problem den Fallback.

    Der Fallback ist kein Notnagel, sondern ein vollstaendiger, versendbarer
    Text — der Nutzer soll nie vor einem leeren Feld stehen, nur weil Vertex
    gerade sein Minutenkontingent voll hat.
    """
    from core.ai.gemini import call_gemini

    prompt = (
        "Du schreibst fuer einen deutschen Handwerksbetrieb eine E-Mail an "
        "einen Kunden.\n\n"
        f"{fakten}\n"
        f"Tonfall: {ton}\n\n"
        "Regeln:\n"
        "- Deutsch, Sie-Form, 4 bis 7 Zeilen.\n"
        "- Nur der Mailtext. KEINE Betreffzeile, KEINE Grussformel am Ende "
        "und keine Unterschrift — die haengt das System selbst an.\n"
        "- Keine erfundenen Zahlen, Fristen oder Zusagen ausser den oben "
        "genannten.\n"
        "- Keine Platzhalter in eckigen Klammern.\n"
    )
    try:
        text = (await call_gemini(
            prompt, temperature=0.4, max_output_tokens=1024,
            tenant_id=str(tid), operation_kind="erinnerung",
        ) or "").strip()
    except Exception:  # noqa: BLE001
        logger.info("erinnerung: Gemini nicht verfuegbar, nutze Fallback", exc_info=True)
        return fallback
    if len(text) < 40 or "[" in text:
        return fallback
    return text


async def senden(
    tid: uuid.UUID, *, to_email: str, betreff: str, text: str,
    employee_id: uuid.UUID | None = None,
) -> dict:
    """Verschickt den (vom Menschen freigegebenen) Entwurf.

    Duenner Wrapper um den vorhandenen Mail-Weg — hier entsteht bewusst
    keine zweite Versand-Implementierung.
    """
    from core.services.mail_compose import send_freie_mail

    return await send_freie_mail(
        tid, to_email=to_email, betreff=betreff, text=text,
        employee_id=employee_id,
    )
