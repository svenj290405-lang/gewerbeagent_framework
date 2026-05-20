"""HTML-Mail-Template fuer professionelle Kunden-Antworten.

Nutzt Tabellen-Layout (E-Mail-Standard, kompatibel mit Outlook + Gmail
+ Apple Mail) statt flexbox/grid. Inline-CSS weil die meisten Mail-
Clients <style>-Tags ignorieren oder strippen.

Anti-Scam-Massnahmen:
- URLs nur in Button-hrefs, NIE als Display-Text. Keine
  "https://..."-Strings im sichtbaren Body.
- Sichtbarer Inhaber-Name + Firma im Header (kein anonymes
  "Support-Team").
- Wenn der Tenant eine Website hat: zweiter Button "Zur Website"
  daneben/darunter, sodass der Empfaenger ueber die offizielle
  Domain den Absender verifizieren kann.

KEINE Versprechen die wir nicht halten koennen — also keine
"DSGVO-konform"-Behauptung im Body und keine "Antwort innerhalb
24 Stunden"-Zusage (was wir nicht garantieren koennen).

Workflow:
- build_kunde_reply_html(kontext) -> str (komplettes HTML)
- extract_first_name(name_or_email) -> str (z.B. "Sven Jantos" -> "Sven")
"""
from __future__ import annotations

import html as _html
import re as _re
from typing import Optional
from urllib.parse import urlparse


def extract_first_name(name_or_email: str) -> str:
    """Extrahiert den Vornamen aus '<Vorname Nachname>' oder fallback auf Mail-Lokalteil.

    Beispiele:
    - "Sven Jantos" -> "Sven"
    - "Dr. Max Müller" -> "Max"
    - "maria.mueller@example.com" -> "Maria"
    - "info@firma.de" -> "" (leer)
    """
    if not name_or_email:
        return ""

    name = name_or_email.strip()

    # Falls noch '<email@x>' im Namen drin: rauschmeissen
    name = _re.sub(r"\s*<[^>]+>", "", name).strip()

    # Wenn ohne @ und mit Leerzeichen -> Anrede-Name
    if "@" not in name and " " in name:
        parts = [p for p in name.split() if not p.endswith(".")]
        if parts:
            return parts[0]

    # Sonst Mail-Lokalteil als Fallback
    if "@" in name:
        local = name.split("@")[0]
        # Generische Adressen wie info@, mail@, hello@ -> leer zurueckgeben
        if local.lower() in {"info", "kontakt", "mail", "hello", "noreply", "no-reply", "service"}:
            return ""
        # "vorname.nachname" oder "vorname_nachname"
        for sep in (".", "_", "-"):
            if sep in local:
                first = local.split(sep)[0]
                return first.capitalize()
        return local.capitalize()

    # Einzelner Name ohne Leerzeichen
    return name.split()[0] if name else ""


def _extract_display_domain(form_url: str) -> str:
    """Holt 'gewerbeagent.de' aus 'https://gewerbeagent.de/anfrage/abc...'.

    Bei www-Prefix wird das www. entfernt damit der Display-Name
    aufgeraeumter wirkt. Bei Subdomains (anfrage.example.com) bleibt
    die Subdomain als Vertrauenssignal stehen.
    """
    try:
        netloc = urlparse(form_url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc or "unserer Website"
    except Exception:
        return "unserer Website"


def build_kunde_reply_html(
    kunde_anrede_name: str,
    kunde_email: str,
    reply_text: str,
    form_url: str,
    company_name: str,
    contact_name: str,
    contact_email: str = "",
    contact_phone: str = "",
    contact_website: str = "",
    with_formular_button: bool = True,
    slot_proposals: list[dict] | None = None,
    booked_termin: dict | None = None,
    storno_summary: dict | None = None,
) -> str:
    """Baut das komplette Mail-HTML mit Header, Body, Button, Footer.

    Args:
        kunde_anrede_name: Vorname des Kunden, z.B. "Sven"
        kunde_email: Mail des Kunden (fuer Disclaimer)
        reply_text: KI-generierter Antwort-Text (Plain-Text mit \\n)
        form_url: URL zum Anfrage-Formular. Wird nur als Button-href
            verwendet, nie als sichtbarer Text. Bei
            with_formular_button=False darf leer/None-aequivalent sein.
        company_name: Name des Tenants, z.B. "PURA Tischler"
        contact_name: Inhaber-Name, z.B. "Daniel Tombers"
        contact_email: optional, fuer Footer
        contact_phone: optional, fuer Footer
        contact_website: optional, fuer Footer
        with_formular_button: True (Default) = CTA-Block + Button
            unter dem Reply-Text. False = reiner Dialog-Reply ohne
            Button (genutzt im Multi-Turn-Pfad, wenn Q noch nicht
            "soweit" ist das Formular zu schicken).
        slot_proposals: optional Liste von Slot-Dicts fuer den
            PROPOSE_SLOTS-Pfad. Format: [{"wochentag": "Do",
            "datum": "22.05.2026", "uhrzeit": "14:00"}, ...]. Werden
            in einer nummerierten Box unter dem Reply gerendert.
            Schliesst sich mit with_formular_button gegenseitig aus
            (Slots ODER Formular-Button, nicht beides).
        booked_termin: optional Dict fuer den BOOK_SLOT-Pfad
            {"datum": "22.05.2026", "uhrzeit": "14:00", "anliegen":
            "..."}. Rendert eine "Termin bestaetigt"-Box.
        storno_summary: optional Dict fuer den CANCEL_TERMIN-Pfad
            {"cancelled_count": 2}. Rendert eine "Termin storniert"-
            Box.
    """
    # Anrede generieren
    if kunde_anrede_name:
        greeting = f"Hallo {_html.escape(kunde_anrede_name)},"
    else:
        greeting = "Hallo,"

    # Reply-Text -> HTML (paragraphs aus Doppel-Newlines, br aus Einzel-Newlines)
    # Erste Zeile koennte schon eine Anrede vom Gemini sein - dann skippen
    text = (reply_text or "").strip()
    # Falls Gemini eine Anrede generiert hat (Hallo X) - rausschneiden weil wir
    # eigene haben
    text_lines = text.split("\n")
    if text_lines and _re.match(r"^(hallo|hi|sehr geehrte|guten tag)\b", text_lines[0].strip().lower()):
        text_lines = text_lines[1:]
        # Auch eventuell leere Zeile danach
        while text_lines and not text_lines[0].strip():
            text_lines = text_lines[1:]
    # Falls Gemini eine Signatur am Ende hat - rausschneiden weil wir eigene haben
    while text_lines and _re.match(
        r"^(viele (gruesse|grüße)|mit freundlichen|beste (gruesse|grüße)|liebe (gruesse|grüße)|(gruesse|grüße)|gruss|gruß|mfg|lg)",
        text_lines[-1].strip().lower(),
    ):
        text_lines = text_lines[:-1]
    # Letzte Zeile koennte Name "Daniel (via Q)" sein - auch raus
    while text_lines and "(via q)" in text_lines[-1].lower():
        text_lines = text_lines[:-1]
    # Trailing-Leere entfernen
    while text_lines and not text_lines[-1].strip():
        text_lines = text_lines[:-1]

    # Schutz gegen LLM-Aussetzer: nackte URLs aus dem Reply-Text rausziehen.
    # Das Anti-Scam-Prinzip steht in der Modul-Doku — URLs gehoeren NUR in
    # den Button-href, nicht in den sichtbaren Body. GMX rendert sonst die
    # URL als grossen blauen Link der mit dem CTA-Button konkurriert.
    url_re = _re.compile(r"https?://\S+", _re.IGNORECASE)
    stripped_lines: list[str] = []
    for ln in text_lines:
        # Zeile die NUR aus einer URL (+ ggf. Whitespace/Doppelpunkt) besteht: droppen
        if url_re.fullmatch(ln.strip().rstrip(".,;:")):
            continue
        # Inline-URLs: weg, doppelte Leerzeichen normalisieren
        ln2 = url_re.sub("", ln)
        ln2 = _re.sub(r"\s{2,}", " ", ln2).strip()
        if ln2:
            stripped_lines.append(ln2)
    text_lines = stripped_lines
    # Erneut Trailing-Leere weg (falls die letzte Zeile nur URL war)
    while text_lines and not text_lines[-1].strip():
        text_lines = text_lines[:-1]

    cleaned = "\n".join(text_lines)

    # Zu HTML
    paragraphs = []
    for block in cleaned.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        # In Block: <br> bei einzelnen \n
        block_html = _html.escape(block).replace("\n", "<br>")
        paragraphs.append(f'<p style="margin: 0 0 16px 0;">{block_html}</p>')
    body_html = "\n".join(paragraphs)

    # Initialen fuer Avatar-Block ("DT" aus "Daniel Tombers")
    initials = "".join(
        p[0].upper() for p in (contact_name or "").split() if p
    )[:2] or "·"

    safe_form_url = _html.escape(form_url or "", quote=True)
    safe_company = _html.escape(company_name)
    safe_contact = _html.escape(contact_name or "")
    safe_initials = _html.escape(initials)

    # Formular-Button-HTML — als Variable, weil er an ZWEI Stellen
    # gerendert werden kann: standalone (reiner Angebots-Fall) ODER
    # zusaetzlich unter einer Termin-Bestaetigung. Im neuen Flow buchen
    # wir zuerst den Termin und schicken das Anfrage-Formular gleich in
    # derselben Bestaetigungs-Mail mit ("Termin steht, fuell zur
    # Vorbereitung noch kurz das Formular aus").
    if booked_termin:
        form_intro = (
            "Damit ich deinen Termin gut vorbereiten kann,<br>"
            "brauche ich noch ein paar kurze Angaben."
        )
    else:
        form_intro = (
            "Ein paar kurze Angaben damit ich dir ein<br>"
            "passendes Angebot vorbereiten kann."
        )
    form_button_html = f'''            <!-- CTA-Block: ein Button zum Formular. -->
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 28px 0 8px 0;">
              <tr>
                <td align="center" style="background-color: #fafafa; border: 1px solid #e4e4e7; border-radius: 10px; padding: 28px 24px;">
                  <p style="margin: 0 0 20px 0; font-size: 14px; color: #52525b;">
                    {form_intro}
                  </p>
                  <a href="{safe_form_url}"
                     style="display: inline-block; background-color: #1e3a8a; color: #ffffff; padding: 14px 36px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px;">
                    Formular ausfüllen
                  </a>
                </td>
              </tr>
            </table>'''

    # CTA-Block: Termin-Bestaetigung darf MIT Formular-Button kombiniert
    # werden (Buchung -> Formular gleich mit). Storno + Slot-Liste bleiben
    # exklusiv (kein Formular dazu). Reine Dialog-Replies (ASK_MORE)
    # bekommen gar keinen Block — sonst widerspricht er dem Reply-Text.
    cta_block = ""
    if booked_termin:
        # Termin gebucht — bestaetigende Box mit Datum/Uhrzeit/Anliegen.
        b_datum = _html.escape((booked_termin.get("datum") or "").strip())
        b_uhrzeit = _html.escape((booked_termin.get("uhrzeit") or "").strip())
        b_anliegen = _html.escape((booked_termin.get("anliegen") or "").strip())
        anliegen_line = (
            f'<div style="margin-top: 6px; font-size: 14px; color: #52525b;">'
            f'{b_anliegen}</div>' if b_anliegen else ""
        )
        cta_block = f'''            <!-- Termin-Bestaetigung -->
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 28px 0 8px 0;">
              <tr>
                <td style="background-color: #ecfdf5; border: 1px solid #6ee7b7; border-radius: 10px; padding: 22px 24px;">
                  <div style="font-size: 13px; color: #047857; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px; margin-bottom: 8px;">
                    Termin bestätigt
                  </div>
                  <div style="font-size: 18px; font-weight: 600; color: #064e3b;">
                    {b_datum} · {b_uhrzeit} Uhr
                  </div>
                  {anliegen_line}
                </td>
              </tr>
            </table>'''
        # Nach der Buchung das Anfrage-Formular gleich mitschicken.
        if with_formular_button:
            cta_block += "\n" + form_button_html
    elif storno_summary:
        # Storno-Bestaetigung
        cnt = int(storno_summary.get("cancelled_count") or 0)
        if cnt == 0:
            head = "Termin nicht gefunden"
            sub = "Wir konnten in unserem Kalender keinen passenden Termin finden."
        elif cnt == 1:
            head = "Termin storniert"
            sub = "Ihr Termin wurde aus dem Kalender entfernt."
        else:
            head = f"{cnt} Termine storniert"
            sub = "Ihre Termine wurden aus dem Kalender entfernt."
        cta_block = f'''            <!-- Storno-Bestaetigung -->
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 28px 0 8px 0;">
              <tr>
                <td style="background-color: #fef2f2; border: 1px solid #fca5a5; border-radius: 10px; padding: 22px 24px;">
                  <div style="font-size: 13px; color: #b91c1c; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px; margin-bottom: 8px;">
                    {_html.escape(head)}
                  </div>
                  <div style="font-size: 14px; color: #7f1d1d;">
                    {_html.escape(sub)}
                  </div>
                </td>
              </tr>
            </table>'''
    elif slot_proposals:
        # Slot-Vorschlaege — durchnummerierte Liste damit der Kunde mit
        # "der zweite passt" antworten kann.
        rows: list[str] = []
        for idx, sl in enumerate(slot_proposals[:6]):
            wt = _html.escape((sl.get("wochentag") or "").strip())
            datum = _html.escape((sl.get("datum") or "").strip())
            uhrzeit = _html.escape((sl.get("uhrzeit") or "").strip())
            wt_block = f"{wt}, " if wt else ""
            label = f"{wt_block}{datum} um {uhrzeit} Uhr"
            rows.append(
                f'<tr><td style="padding: 6px 0; font-size: 15px; color: #18181b;">'
                f'<span style="display: inline-block; width: 28px; color: #64748b; font-variant-numeric: tabular-nums;">{idx+1}.</span>'
                f'{label}</td></tr>'
            )
        rows_html = "\n".join(rows)
        cta_block = f'''            <!-- Slot-Vorschlaege -->
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 28px 0 8px 0;">
              <tr>
                <td style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 20px 24px;">
                  <div style="font-size: 13px; color: #475569; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px; margin-bottom: 10px;">
                    Mögliche Termine
                  </div>
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                    {rows_html}
                  </table>
                  <p style="margin: 12px 0 0 0; font-size: 13px; color: #64748b;">
                    Bitte einfach auf die Mail antworten welcher passt.
                  </p>
                </td>
              </tr>
            </table>'''
    elif with_formular_button:
        cta_block = form_button_html

    # Footer-Telefon (unauffällig, optional). contact_website wird
    # bewusst NICHT verlinkt — der Empfänger sieht den Tenant-Namen
    # und kann selbst nach der offiziellen Website googeln. Ein Button
    # "Zur Website" macht die Mail überladen (User-Feedback 2026-05-17).
    phone_row = (
        f'<br><span style="color: #a1a1aa;">{_html.escape(contact_phone)}</span>'
        if contact_phone else ""
    )

    # Anti-Spam-Anpassungen (Layout bleibt unveraendert):
    # - Kein hidden Preheader-div mehr (typischer Marketing-/Newsletter-
    #   Pattern, triggert SpamAssassin "HTML_FONT_LOW_CONTRAST" + Co)
    # - Kein "Verfasst mit Hilfe von Q"-Footer mehr (Auto-Bot-Signal)
    # - <title>-Tag generisch ohne Marketing-Wording
    html = f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin: 0; padding: 0; background-color: #f4f4f5; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #18181b; line-height: 1.5;">

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f4f4f5; padding: 28px 12px;">
  <tr>
    <td align="center">

      <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="max-width: 560px; width: 100%; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 2px rgba(0,0,0,0.04);">

        <!-- Header: Avatar + Name + Firma -->
        <tr>
          <td style="padding: 28px 32px 24px 32px; background-color: #ffffff; border-bottom: 1px solid #f1f5f9;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td valign="middle" width="56" style="padding-right: 16px;">
                  <div style="width: 48px; height: 48px; border-radius: 50%; background-color: #1e3a8a; color: #ffffff; font-size: 18px; font-weight: 600; text-align: center; line-height: 48px;">
                    {safe_initials}
                  </div>
                </td>
                <td valign="middle">
                  <div style="font-size: 16px; font-weight: 600; color: #18181b; letter-spacing: -0.2px;">
                    {safe_contact}
                  </div>
                  <div style="font-size: 13px; color: #71717a; margin-top: 2px;">
                    {safe_company}
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding: 28px 32px 24px 32px;">
            <p style="margin: 0 0 18px 0; font-size: 16px; color: #18181b;">
              {greeting}
            </p>

            <div style="font-size: 15px; line-height: 1.65; color: #3f3f46;">
              {body_html}
            </div>

{cta_block}

          </td>
        </tr>

        <!-- Signatur-Block -->
        <tr>
          <td style="padding: 0 32px 28px 32px;">
            <p style="margin: 0 0 4px 0; font-size: 15px; color: #3f3f46;">
              Bei Rückfragen einfach auf diese Mail antworten.
            </p>
            <p style="margin: 14px 0 0 0; font-size: 13px; color: #71717a;">
              {safe_contact} &middot; {safe_company}{phone_row}
            </p>
          </td>
        </tr>

      </table>

    </td>
  </tr>
</table>

</body>
</html>"""
    return html
