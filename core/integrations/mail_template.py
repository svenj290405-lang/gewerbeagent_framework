"""HTML-Mail-Template fuer professionelle Kunden-Antworten.

Nutzt Tabellen-Layout (E-Mail-Standard, kompatibel mit Outlook + Gmail
+ Apple Mail) statt flexbox/grid. Inline-CSS weil die meisten Mail-
Clients <style>-Tags ignorieren oder strippen.

Anti-Scam-Massnahmen (2026-05-17 Refactor):
- URL wird im Text NICHT als roher Token-Link gezeigt
  (`https://...anfrage/AbC123XyZ`), sondern nur als Domain-Hint
  ("auf gewerbeagent.de"). Der eigentliche Token-Link sitzt im
  Button-href + im Hover-Preview, das genuegt fuer Transparenz.
- Trust-Box mit drei Vertrauens-Signalen unter dem CTA-Button.
- Sichtbarer Inhaber-Name oben + im Footer (kein anonymes "Support-Team").
- Datenschutz-Hinweis + Antwortzeit-Versprechen im Footer.

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
) -> str:
    """Baut das komplette Mail-HTML mit Header, Body, Button, Footer.

    Args:
        kunde_anrede_name: Vorname des Kunden, z.B. "Sven"
        kunde_email: Mail des Kunden (fuer Disclaimer)
        reply_text: KI-generierter Antwort-Text (Plain-Text mit \\n)
        form_url: URL zum Anfrage-Formular
        company_name: Name des Tenants, z.B. "PURA Tischler"
        contact_name: Inhaber-Name, z.B. "Daniel Tombers"
        contact_email: optional, fuer Footer
        contact_phone: optional, fuer Footer
        contact_website: optional, fuer Footer
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
    if text_lines and _re.match(r"^(hallo|hi|sehr geehrte|guten tag)", text_lines[0].strip().lower()):
        text_lines = text_lines[1:]
        # Auch eventuell leere Zeile danach
        while text_lines and not text_lines[0].strip():
            text_lines = text_lines[1:]
    # Falls Gemini eine Signatur am Ende hat - rausschneiden weil wir eigene haben
    while text_lines and _re.match(
        r"^(viele gruesse|mit freundlichen|beste gruesse|liebe gruesse|gruesse|gruss|mfg|lg)",
        text_lines[-1].strip().lower(),
    ):
        text_lines = text_lines[:-1]
    # Letzte Zeile koennte Name "Daniel (via Q)" sein - auch raus
    while text_lines and "(via q)" in text_lines[-1].lower():
        text_lines = text_lines[:-1]
    # Trailing-Leere entfernen
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

    safe_form_url = _html.escape(form_url, quote=True)
    display_domain = _extract_display_domain(form_url)
    safe_display_domain = _html.escape(display_domain)
    safe_company = _html.escape(company_name)
    safe_contact = _html.escape(contact_name or "")
    safe_initials = _html.escape(initials)

    # Footer-Kontakt-Zeile (Telefon / Mail / Website) — nur was da ist
    contact_chips = []
    if contact_phone:
        contact_chips.append(
            f'<span style="color: #6b7280;">📞 {_html.escape(contact_phone)}</span>'
        )
    if contact_email:
        contact_chips.append(
            f'<a href="mailto:{_html.escape(contact_email, quote=True)}" '
            f'style="color: #6b7280; text-decoration: none;">'
            f'✉ {_html.escape(contact_email)}</a>'
        )
    if contact_website:
        # http(s) prefix falls fehlt damit Link klickbar bleibt
        href = contact_website if contact_website.startswith(("http://", "https://")) \
            else f"https://{contact_website}"
        contact_chips.append(
            f'<a href="{_html.escape(href, quote=True)}" '
            f'style="color: #6b7280; text-decoration: none;">'
            f'🌐 {_html.escape(contact_website)}</a>'
        )
    contact_row = (
        '<br>' + ' &middot; '.join(contact_chips) if contact_chips else ""
    )

    html = f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Antwort von {safe_company}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f4f4f5; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #18181b; line-height: 1.5;">

<!-- Preheader: in der Inbox-Vorschau sichtbar, im Body unsichtbar -->
<div style="display: none; max-height: 0; overflow: hidden; visibility: hidden; mso-hide: all; font-size: 1px; line-height: 1px; color: #f4f4f5;">
  Dein Anfrage-Formular von {safe_company} &mdash; bitte kurz ausfuellen damit {safe_contact} dir ein passendes Angebot machen kann.
</div>

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

            <!-- CTA-Block -->
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 28px 0 8px 0;">
              <tr>
                <td align="center" style="background-color: #fafafa; border: 1px solid #e4e4e7; border-radius: 10px; padding: 28px 24px;">
                  <p style="margin: 0 0 8px 0; font-size: 16px; color: #18181b; font-weight: 600;">
                    Dein Anfrage-Formular
                  </p>
                  <p style="margin: 0 0 20px 0; font-size: 14px; color: #52525b;">
                    Ein paar kurze Angaben damit ich dir ein<br>
                    passendes Angebot vorbereiten kann.
                  </p>
                  <a href="{safe_form_url}"
                     style="display: inline-block; background-color: #1e3a8a; color: #ffffff; padding: 14px 36px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px;">
                    Formular oeffnen
                  </a>
                  <p style="margin: 14px 0 0 0; font-size: 12px; color: #71717a;">
                    auf <strong style="color: #3f3f46;">{safe_display_domain}</strong>
                  </p>
                </td>
              </tr>
            </table>

            <!-- Trust-Box: drei kurze Vertrauens-Signale -->
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 0 0 8px 0;">
              <tr>
                <td align="center" style="padding: 6px 0; font-size: 12px; color: #71717a;">
                  &#10003; Persoenlich fuer dich &nbsp;&middot;&nbsp;
                  &#10003; DSGVO-konform &nbsp;&middot;&nbsp;
                  &#10003; Antwort innerhalb 24 h
                </td>
              </tr>
            </table>

          </td>
        </tr>

        <!-- Signatur-Block -->
        <tr>
          <td style="padding: 0 32px 28px 32px;">
            <p style="margin: 0 0 4px 0; font-size: 15px; color: #3f3f46;">
              Bei Rueckfragen einfach auf diese Mail antworten &ndash;
            </p>
            <p style="margin: 0 0 4px 0; font-size: 15px; color: #3f3f46;">
              {safe_contact} liest mit.
            </p>
            <p style="margin: 14px 0 0 0; font-size: 13px; color: #71717a;">
              {safe_company}{contact_row}
            </p>
          </td>
        </tr>

        <!-- Footer / Disclaimer -->
        <tr>
          <td style="background-color: #fafafa; padding: 16px 32px; border-top: 1px solid #f1f5f9;">
            <p style="margin: 0; font-size: 11px; color: #a1a1aa; line-height: 1.5;">
              Diese Antwort wurde mit Hilfe von <strong style="color: #71717a;">Q</strong>, dem digitalen Assistenten von
              {safe_company}, erstellt. {safe_contact} liest jede Antwort mit.
              Deine Daten werden ausschliesslich zur Beantwortung deiner Anfrage verwendet.
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
