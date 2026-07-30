"""Kundengespraech — der Arbeitsbereich rund um ein Gespraech beim Kunden.

Ein Gespraech buendelt, was vor Ort anfaellt: das Diktat (KI-Briefing,
Notizen, To-dos, Termin), die getippte Handnotiz, Fotos aus dem
Drive-Kundenordner und im Q-Chat gerenderte Visualisierungen. Am Ende
steht die Kundenmail.

**Die Vertraulichkeitsgrenze steht hier und nirgends sonst.** Alles, was
der Handwerker fuer sich festhaelt, bleibt bei ihm; in die Kundenmail geht
ausschliesslich, was den Kunden angeht:

    ins Gespraech, NICHT in die Mail : Transkript, Handnotiz, To-dos,
                                       Preise/Betraege, Confidence
    darf in die Mail                 : Kurz-Briefing, lange Notizen,
                                       Termin, ausgewaehlte Bilder

``baue_kundenmail`` fuettert Gemini deshalb gar nicht erst mit dem
Transkript oder der Handnotiz — was nicht im Prompt steht, kann auch nicht
versehentlich in der Mail landen. Der Entwurf geht danach zur Redaktion an
den Handwerker (Freigabe wie bei jeder anderen Mail).
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

logger = logging.getLogger(__name__)

# Was Gemini fuer die Kundenmail sehen darf. Bewusst als Konstante, damit
# beim Erweitern auffaellt, dass man an der Vertraulichkeitsgrenze schraubt.
MAIL_QUELLEN = ("briefing_kurz", "notizen_lang", "termin")


def _mail_prompt(*, betrieb: str, kunde: str, briefing: str,
                 notizen: str, termin: str, bilder: int) -> str:
    return (
        f"Du schreibst im Namen des Handwerksbetriebs „{betrieb}\" eine kurze "
        f"Mail an den Kunden {kunde} — als Nachbereitung des Gespraechs.\n\n"
        "Schreibe hoeflich in der Sie-Form, 4-8 Saetze, ohne Aufzaehlungs"
        "zeichen, ohne Markdown. Beginne mit einer passenden Anrede und ende "
        "mit einer Grussformel im Namen des Betriebs.\n\n"
        "INHALT: Fasse zusammen, was besprochen und vereinbart wurde, und "
        "was als Naechstes passiert. "
        + (f"Erwaehne den vereinbarten Termin: {termin}. " if termin else "")
        + (f"Weise darauf hin, dass {bilder} Bild(er) im Anhang liegen. "
           if bilder else "")
        + "\n\nSTRENG VERBOTEN — das ist eine Kundenmail, keine interne Notiz:\n"
        "- KEINE Preise, Betraege, Stundensaetze oder Kostenschaetzungen "
        "(darueber entscheidet der Betrieb spaeter im Angebot).\n"
        "- KEINE internen Einschaetzungen oder Bewertungen des Kunden.\n"
        "- KEINE To-do-Formulierungen („ich muss noch…\").\n"
        "- Nichts erfinden: nur was unten steht.\n\n"
        f"Gespraechs-Zusammenfassung:\n{briefing}\n\n"
        f"Weitere Notizen:\n{notizen}\n"
    )


def _fallback_text(*, betrieb: str, kunde: str, briefing: str,
                   termin: str, bilder: int) -> str:
    """Wenn Gemini ausfaellt, steht trotzdem ein Entwurf da — der
    Handwerker redigiert ihn ohnehin."""
    teile = [f"Guten Tag {kunde},", ""]
    teile.append(briefing.strip() or
                 "vielen Dank fuer das Gespraech. Hier eine kurze "
                 "Zusammenfassung unserer Absprache.")
    if termin:
        teile.append("")
        teile.append(f"Vereinbarter Termin: {termin}.")
    if bilder:
        teile.append("")
        teile.append("Die besprochenen Bilder finden Sie im Anhang.")
    teile += ["", "Mit freundlichen Gruessen", betrieb]
    return "\n".join(teile)


async def _als_drive_anhang(
    tid: uuid.UUID, kunde_name: str, datei, employee_id: uuid.UUID | None,
) -> dict | None:
    """Macht aus einer Gespraechs-Datei einen Drive-Anhang.

    Fotos liegen bereits im Kundenordner — da reicht die Datei-ID.
    Visualisierungen liegen als Bytes in der DB; die werden beim Anhaengen
    in den Kundenordner geladen. Das ist kein Umweg, sondern gewollt: was
    beim Kunden landet, gehoert auch in seinen Ordner.
    """
    from core.models.gespraech_datei import GESPRAECH_DATEI_VISUALISIERUNG

    if datei.drive_file_id:
        return {"quelle": "drive", "id": datei.drive_file_id,
                "name": datei.dateiname or "Foto.jpg"}

    if datei.typ != GESPRAECH_DATEI_VISUALISIERUNG or not datei.visualisierung_id:
        return None

    from core.database.connection import get_session
    from sqlalchemy import select
    from core.models.visualisierung import Visualisierung
    from core.integrations.google_drive import upload_file_to_kunde_folder

    async with get_session() as s:
        viz = (await s.execute(
            select(Visualisierung).where(
                Visualisierung.id == datei.visualisierung_id,
                Visualisierung.tenant_id == tid,
            ))).scalar_one_or_none()
        bytes_ = viz.result_image_data if viz else None
    if not bytes_:
        return None

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = datei.dateiname or f"visualisierung_{ts}.png"
    try:
        res = await upload_file_to_kunde_folder(
            tenant_id=tid, kunde_name=kunde_name, file_bytes=bytes_,
            filename=name, mime_type="image/png", employee_id=employee_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Visualisierung nicht in den Kundenordner ladbar: %s", exc)
        return None

    # Datei-ID merken, damit dasselbe Bild beim naechsten Mal direkt
    # angehaengt werden kann statt erneut hochgeladen zu werden.
    async with get_session() as s:
        from core.models.gespraech_datei import GespraechDatei
        row = (await s.execute(
            select(GespraechDatei).where(GespraechDatei.id == datei.id)
        )).scalar_one_or_none()
        if row is not None:
            row.drive_file_id = res.get("file_id")
            row.drive_url = res.get("web_link")
            await s.commit()
    return {"quelle": "drive", "id": res.get("file_id"), "name": name}


async def baue_kundenmail(
    tid: uuid.UUID, *, gespraech, bilder: list, betrieb: str,
    employee_id: uuid.UUID | None = None,
) -> dict:
    """Baut den Mail-Entwurf zu einem Gespraech (Karte im Assistenten).

    ``bilder`` sind die vom Handwerker ausgewaehlten GespraechDatei-Zeilen.
    Rueckgabe hat dieselbe Form wie der Entwurf aus dem Assistenten
    (``type: email_entwurf``), damit die App nur EINE Entwurfs-Karte kennt.
    """
    from core.ai.gemini import call_gemini
    from core.services.mail_compose import (
        EMAIL_RE, MAX_BETREFF, MAX_TEXT, lookup_kunde_email)

    kunde = (gespraech.kunde_name or "").strip()
    briefing = (gespraech.briefing_kurz or "").strip()
    notizen = (gespraech.notizen_lang or "").strip()
    termin = ""
    if gespraech.termin_datum:
        termin = gespraech.termin_datum.strftime("%d.%m.%Y um %H:%M Uhr")
        if gespraech.termin_ort:
            termin += f" ({gespraech.termin_ort})"

    anhaenge: list[dict] = []
    for d in bilder:
        a = await _als_drive_anhang(tid, kunde, d, employee_id)
        if a and a.get("id"):
            anhaenge.append(a)

    text = ""
    if briefing or notizen:
        try:
            text = (await call_gemini(
                _mail_prompt(betrieb=betrieb, kunde=kunde, briefing=briefing,
                             notizen=notizen, termin=termin,
                             bilder=len(anhaenge)),
                temperature=0.5, max_output_tokens=2048,
                tenant_id=str(tid), operation_kind="gespraech_mail",
            )).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kundenmail-Gemini fehlgeschlagen: %s", exc)
    if not text:
        text = _fallback_text(betrieb=betrieb, kunde=kunde, briefing=briefing,
                              termin=termin, bilder=len(anhaenge))

    empfaenger = (gespraech.kunde_email or "").strip() if hasattr(
        gespraech, "kunde_email") else ""
    if not empfaenger and kunde:
        empfaenger = await lookup_kunde_email(tid, kunde) or ""
    hinweis = None
    if not empfaenger:
        hinweis = "Ich habe keine Adresse gefunden — bitte trag sie ein."
    elif not EMAIL_RE.match(empfaenger):
        hinweis = "Die Adresse sieht nicht vollständig aus — bitte prüfen."

    fehlend = len(bilder) - len(anhaenge)
    if fehlend > 0:
        zusatz = (f"{fehlend} Bild(er) konnten nicht angehängt werden "
                  "(Drive nicht verbunden?).")
        hinweis = f"{hinweis} {zusatz}" if hinweis else zusatz

    return {
        "type": "email_entwurf",
        "tool": "email_schreiben",
        "empfaenger": empfaenger,
        "empfaenger_name": kunde,
        "betreff": (f"Unser Gespräch — {betrieb}" if betrieb
                    else "Unser Gespräch")[:MAX_BETREFF],
        "text": text[:MAX_TEXT],
        "kunde_name": kunde,
        "anhaenge": anhaenge,
        "hinweis": hinweis,
    }
